import httpx
from click.testing import CliRunner

import augury.cli.sources as cli_sources
from augury.cli import main
from augury.core.db.discovery_repo import DiscoveryRepo
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from tests.adapters.test_probe import ORIGIN as BLOG
from tests.adapters.test_probe import PAGE_WITHOUT_LINK
from tests.adapters.test_rss import FEED
from tests.agents.discovery.test_agent import call
from tests.helpers import ScriptedLlm, allow_robots, fake_resolver

FEED_URL = f"{BLOG}/feed.xml"
RSS = {"type": "rss", "feed_url": FEED_URL}


def submit(*urls: str):
    return call(
        "submit_candidates",
        candidates=[
            {"name": f"Blog {i}", "homepage": f"{BLOG}/", "recipe": {"type": "rss", "feed_url": u}}
            for i, u in enumerate(urls, start=1)
        ],
    )


def use_model(monkeypatch, llm: ScriptedLlm) -> None:
    monkeypatch.setattr(cli_sources, "default_resolver", lambda config: fake_resolver(llm))


def serve(respx_mock) -> None:
    allow_robots(respx_mock, BLOG)
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, content=FEED))
    respx_mock.get(f"{BLOG}/other.xml").mock(return_value=httpx.Response(200, content=FEED))


def test_a_name_is_discovered_tested_and_added_after_confirming(fast_http, respx_mock, monkeypatch):
    serve(respx_mock)
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit(FEED_URL)])
    use_model(monkeypatch, llm)
    result = CliRunner().invoke(main, ["sources", "add", "example blog"], input="1\n")
    assert result.exit_code == 0, result.output
    assert "→ test_recipe rss" in result.output and "✓ 3 items" in result.output
    assert "1. Blog 1  (rss: " in result.output and "· First & best" in result.output
    conn = open_db(fast_http)
    source_id = SourcesRepo(conn).find_by_recipe_url(FEED_URL)
    assert source_id == "blog-1"
    run = RunsRepo(conn).last("discovery")
    assert run is not None and DiscoveryRepo(conn).get(run.id).chosen  # type: ignore[union-attr]


def test_zero_adds_nothing(fast_http, respx_mock, monkeypatch):
    serve(respx_mock)
    use_model(monkeypatch, ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit(FEED_URL)]))
    result = CliRunner().invoke(main, ["sources", "add", "example blog"], input="0\n")
    assert result.exit_code == 0 and "Nothing added." in result.output
    assert SourcesRepo(open_db(fast_http)).find_by_recipe_url(FEED_URL) is None


def test_two_candidates_can_both_be_added(fast_http, respx_mock, monkeypatch):
    serve(respx_mock)
    other = {"type": "rss", "feed_url": f"{BLOG}/other.xml"}
    replies = [
        call("test_recipe", recipe=RSS),
        call("test_recipe", recipe=other),
        submit(FEED_URL, f"{BLOG}/other.xml"),
    ]
    use_model(monkeypatch, ScriptedLlm(replies=replies))
    result = CliRunner().invoke(main, ["sources", "add", "example blog"], input="1, 2\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Added blog-") == 2


def test_a_url_without_a_feed_falls_back_to_the_agent(fast_http, respx_mock, monkeypatch):
    hidden = f"{BLOG}/posts/latest.rss"  # at no path the probe tries
    allow_robots(respx_mock, BLOG)
    respx_mock.get(hidden).mock(return_value=httpx.Response(200, content=FEED))
    respx_mock.get(f"{BLOG}/").mock(return_value=httpx.Response(200, content=PAGE_WITHOUT_LINK))
    respx_mock.route().mock(return_value=httpx.Response(404))
    recipe = {"type": "rss", "feed_url": hidden}
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=recipe), submit(hidden)])
    use_model(monkeypatch, llm)
    result = CliRunner().invoke(main, ["sources", "add", f"{BLOG}/", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Asking the discovery agent" in result.output and "Added blog-1" in result.output
    assert "The code probe found no valid feed" in llm.prompts[0]


def test_without_a_model_a_name_explains_why(fast_http):
    result = CliRunner().invoke(main, ["sources", "add", "example blog", "--yes"])
    assert result.exit_code != 0 and "smart model" in result.output and "--rss" in result.output


def test_a_duplicate_is_shown_but_not_added(fast_http, respx_mock, monkeypatch):
    serve(respx_mock)
    CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    use_model(monkeypatch, ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit(FEED_URL)]))
    result = CliRunner().invoke(main, ["sources", "add", "example blog", "--yes"])
    assert "[duplicate of" in result.output and result.exit_code != 0
    assert "no new source" in result.output


def test_a_run_can_be_replayed_from_sessions_db(fast_http, respx_mock, monkeypatch):
    serve(respx_mock)
    use_model(monkeypatch, ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit(FEED_URL)]))
    CliRunner().invoke(main, ["sources", "add", "example blog", "--yes"])
    run = RunsRepo(open_db(fast_http)).last("discovery")
    assert run is not None
    result = CliRunner().invoke(main, ["dev", "discovery", run.id])
    assert result.exit_code == 0, result.output
    assert "→ test_recipe" in result.output and "← submit_candidates" in result.output


def test_doctor_names_the_search_provider(paths, monkeypatch):
    import augury.cli

    monkeypatch.setattr(augury.cli, "default_resolver", lambda config: fake_resolver())
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "search" in result.output and "duckduckgo (auto)" in result.output
    paths.config_file.write_text('[search]\nprovider = "tavily"\n')
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "TAVILY_API_KEY" in result.output
