import asyncio

from rich.text import Text
from textual.widgets import Input

from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.models import HfBlogRecipe, RssRecipe, SitemapRecipe, Source
from augury.tui.widgets.add_source_panel import AddSourcePanel
from augury.tui.widgets.discovery_panel import CandidateList, DiscoveryPanel
from augury.tui.widgets.source_detail import SourceDetail
from augury.tui.widgets.sources_table import SourcesTable
from tests.adapters.test_rss import FEED
from tests.agents.discovery.test_agent import call
from tests.helpers import CountingHttp, ScriptedLlm, SlowHttp, fake_resolver
from tests.tui.conftest import NOW, Gate, until

FEED_URL = "https://blog.example.com/feed.xml"
RSS = {"type": "rss", "feed_url": FEED_URL}


def submit(url: str = FEED_URL):
    return call(
        "submit_candidates",
        candidates=[{"name": "Example Eng", "recipe": {"type": "rss", "feed_url": url}}],
    )


def panel_text(app) -> str:
    panel = app.query_one(DiscoveryPanel)
    parts = [str(w.render()) for w in panel.query("Static")]
    return "\n".join(parts)


async def type_and_enter(app, pilot, value: str) -> None:
    await pilot.press("2", "plus")
    app.query_one("#add-url", Input).value = value
    await pilot.press("enter")


async def test_a_name_shows_live_progress_then_a_checklist_and_enter_adds(make_app):
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit()])
    app = make_app(http=CountingHttp({FEED_URL: FEED}), resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: app.query_one(DiscoveryPanel).candidates)
        text = panel_text(app)
        assert "→ test_recipe rss" in text and "✓ 3 items" in text
        assert "1 candidate(s)" in text
        options = app.query_one(CandidateList)
        assert app.focused is options and options.checked == {0}  # the first is pre-checked
        await pilot.press("enter")
        await until(pilot, lambda: not app.query_one(DiscoveryPanel).display)
        assert SourcesRepo(app.conn).find_by_recipe_url(FEED_URL) == "example-eng"
        assert app.query_one(SourcesTable).row_count == 4
        assert app.query_one(SourceDetail).display


async def test_space_unchecks_so_enter_adds_nothing(make_app):
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit()])
    app = make_app(http=CountingHttp({FEED_URL: FEED}), resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: app.query_one(DiscoveryPanel).candidates)
        await pilot.press("space", "enter")
        await pilot.pause()
        assert app.query_one(DiscoveryPanel).display  # still open, with a hint
        assert SourcesRepo(app.conn).find_by_recipe_url(FEED_URL) is None


async def test_without_a_model_the_panel_says_why(make_app):
    app = make_app(http=CountingHttp(), resolver=fake_resolver(unavailable="no key set"))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: "No candidates" in panel_text(app))
        assert "no key set" in panel_text(app)


async def test_escape_cancels_a_running_discovery(make_app):
    http = SlowHttp()
    llm = ScriptedLlm(replies=[call("fetch_page", url="https://blog.example.com/")])
    app = make_app(http=http, resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: http.started.is_set())
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        assert not app.query_one(DiscoveryPanel).display
        run = RunsRepo(app.conn).last("discovery")
        assert run is not None and run.status == "interrupted"


async def test_r_rediscovers_a_broken_source_and_enter_replaces_its_recipe(make_app):
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit()])
    app = make_app(http=CountingHttp({FEED_URL: FEED}), resolver=fake_resolver(llm))
    old = SitemapRecipe(sitemap_url="https://blog.example.com/sitemap.xml", include_pattern="/")
    repo = SourcesRepo(app.conn)
    repo.add(
        Source(
            id="blog", name="Blog", homepage="https://blog.example.com/", origin="user", recipe=old
        ),
        now=NOW,
    )
    for _ in range(3):
        repo.record_failure("blog", "AdapterError: 0 URLs matching", now=NOW)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        app.query_one(SourcesTable).select_key("blog")
        await pilot.press("R")
        await until(pilot, lambda: app.query_one(DiscoveryPanel).candidates)
        assert "Enter replaces blog's recipe" in panel_text(app)
        await pilot.press("enter")
        await until(pilot, lambda: not app.query_one(DiscoveryPanel).display)
        record = repo.get("blog")
        assert record is not None and record.source.recipe == RssRecipe(feed_url=FEED_URL)
        assert record.health == "never" and record.consecutive_failures == 0


async def test_r_on_a_builtin_source_is_refused(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "R")
        await pilot.pause()
        assert not app.query_one(DiscoveryPanel).display
        assert any("can't be re-discovered" in n.message for n in app._notifications)


async def test_hostile_candidate_text_is_shown_literally(make_app):
    hostile_feed = FEED.replace(b"First &amp; best", b"[bold red]pwned[/] \x1b[31mx")
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit()])
    app = make_app(http=CountingHttp({FEED_URL: hostile_feed}), resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: app.query_one(DiscoveryPanel).candidates)
        prompt = app.query_one(CandidateList).get_option_at_index(0).prompt
        assert isinstance(prompt, Text)
        assert "[bold red]pwned[/]" in prompt.plain and "\x1b" not in prompt.plain
        assert "[x] Example Eng" in prompt.plain


async def test_quitting_mid_discovery_is_clean(make_app):
    http = SlowHttp()
    llm = ScriptedLlm(replies=[call("fetch_page", url="https://blog.example.com/")])
    app = make_app(http=http, resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: http.started.is_set())
        await pilot.press("q")
    run = RunsRepo(app.conn).last("discovery")
    assert run is not None and run.status == "interrupted"


# Carried-over ruling (Task 33): with neither a web homepage nor a recipe URL,
# rediscover_target falls back to the source's *name*, which is never fetched as a URL.


def add_urlless_source(app, name: str) -> None:
    repo = SourcesRepo(app.conn)
    repo.add(Source(id="papers", name=name, origin="user", recipe=HfBlogRecipe()), now=NOW)


async def test_r_without_a_homepage_or_recipe_url_discovers_by_name(make_app):
    http = CountingHttp()
    app = make_app(http=http, resolver=fake_resolver(unavailable="no key set"))
    add_urlless_source(app, "Paper Picks")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        app.query_one(SourcesTable).select_key("papers")
        await pilot.press("R")
        await until(pilot, lambda: "No candidates" in panel_text(app))
        assert app.query_one(DiscoveryPanel).rediscover_id == "papers"
        assert "no key set" in panel_text(app)
        assert http.calls == []  # a name is searched for by the agent, never fetched


async def test_r_refuses_a_name_that_looks_like_an_address(make_app):
    http = CountingHttp()
    app = make_app(http=http, resolver=fake_resolver(unavailable="no key set"))
    add_urlless_source(app, "papers.example.com")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        app.query_one(SourcesTable).select_key("papers")
        await pilot.press("R")
        await pilot.pause()
        assert not app.query_one(DiscoveryPanel).display
        assert any("can't be re-discovered" in n.message for n in app._notifications)
        assert http.calls == []


class GatedHttp(CountingHttp):
    """Holds every fetch until the test opens the gate, so the run ends when the test says."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        super().__init__(pages)
        self.gate = Gate()
        self.started = asyncio.Event()

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        self.started.set()
        await self.gate()
        return await super().get(url)


async def test_a_run_that_ends_in_another_view_does_not_take_the_focus(make_app):
    http = GatedHttp({FEED_URL: FEED})
    llm = ScriptedLlm(replies=[call("test_recipe", recipe=RSS), submit()])
    app = make_app(http=http, resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: http.started.is_set())  # test_recipe is fetching the feed
        await pilot.press("1")  # back to the items while the agent works
        http.gate.open()
        await until(pilot, lambda: app.query_one(DiscoveryPanel).candidates)
        await pilot.pause()
        assert not isinstance(app.focused, CandidateList)  # Enter there would add unseen
        await pilot.press("enter")
        assert SourcesRepo(app.conn).find_by_recipe_url(FEED_URL) is None
        await pilot.press("2")  # the checklist is still there, pre-checked, to go back to
        assert app.query_one(CandidateList).checked == {0}


async def test_plus_while_discovering_cancels_the_run_and_opens_the_add_panel(make_app):
    http = SlowHttp()
    llm = ScriptedLlm(replies=[call("fetch_page", url="https://blog.example.com/")])
    app = make_app(http=http, resolver=fake_resolver(llm))
    async with app.run_test(size=(160, 40)) as pilot:
        await type_and_enter(app, pilot, "example engineering")
        await until(pilot, lambda: http.started.is_set())
        await pilot.press("plus")  # try another name instead
        await app.workers.wait_for_complete()
        assert not app.query_one(DiscoveryPanel).display
        assert app.query_one(AddSourcePanel).display
        assert app.focused is app.query_one("#add-url", Input)
        run = RunsRepo(app.conn).last("discovery")
        assert run is not None and run.status == "interrupted"
