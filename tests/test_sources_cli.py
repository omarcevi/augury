from datetime import UTC, datetime

import httpx
from click.testing import CliRunner

from augury.agents.scout import ScoutReport, SourceStats
from augury.cli import format_report, main
from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourcesRepo
from tests.adapters.test_probe import ORIGIN as BLOG
from tests.adapters.test_probe import PAGE_WITH_LINK, PAGE_WITHOUT_LINK
from tests.adapters.test_rss import FEED, FEED_URL, ORIGIN
from tests.helpers import allow_robots


def _serve_feed(respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, content=FEED))


def test_add_rss_then_list(fast_http, respx_mock):
    _serve_feed(respx_mock)
    added = CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    assert added.exit_code == 0, added.output
    assert "Added example-blog" in added.output and "· First & best" in added.output
    listed = CliRunner().invoke(main, ["sources", "list"])
    assert "example-blog" in listed.output and "rss" in listed.output


def test_adding_the_same_feed_twice_is_refused(fast_http, respx_mock):
    _serve_feed(respx_mock)
    CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    again = CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    assert again.exit_code != 0 and "already added" in again.output


def test_builtin_sources_cannot_be_removed(paths):
    result = CliRunner().invoke(main, ["sources", "remove", "hf-papers", "--yes"])
    assert result.exit_code != 0 and "disable it instead" in result.output


def test_disable_and_enable(paths):
    assert CliRunner().invoke(main, ["sources", "disable", "hf-community"]).exit_code == 0
    assert "off" in CliRunner().invoke(main, ["sources", "list"]).output
    assert CliRunner().invoke(main, ["sources", "enable", "hf-community"]).exit_code == 0


def test_test_command_prints_samples_without_storing(fast_http, respx_mock):
    _serve_feed(respx_mock)
    CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    result = CliRunner().invoke(main, ["sources", "test", "example-blog"])
    assert result.exit_code == 0 and "3 items" in result.output and "First & best" in result.output


def test_add_by_page_url(fast_http, respx_mock):
    allow_robots(respx_mock, BLOG)
    respx_mock.get(f"{BLOG}/").mock(return_value=httpx.Response(200, content=PAGE_WITH_LINK))
    respx_mock.get(f"{BLOG}/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = CliRunner().invoke(main, ["sources", "add", f"{BLOG}/", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Added example-blog" in result.output


def test_page_without_a_feed_explains_what_was_tried(fast_http, respx_mock):
    allow_robots(respx_mock, BLOG)
    respx_mock.get(f"{BLOG}/").mock(return_value=httpx.Response(200, content=PAGE_WITHOUT_LINK))
    respx_mock.route().mock(return_value=httpx.Response(404))
    result = CliRunner().invoke(main, ["sources", "add", f"{BLOG}/", "--yes"])
    assert result.exit_code != 0
    assert "tried" in result.output and "--rss" in result.output


def test_a_bare_domain_is_probed_over_https(fast_http, respx_mock):
    allow_robots(respx_mock, "https://jvns.ca")
    page = respx_mock.get("https://jvns.ca/").mock(
        return_value=httpx.Response(200, content=PAGE_WITH_LINK)
    )
    respx_mock.get("https://jvns.ca/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = CliRunner().invoke(main, ["sources", "add", "jvns.ca", "--yes"])
    assert result.exit_code == 0, result.output
    assert page.called and "Added example-blog" in result.output


def test_a_non_web_address_is_refused_clearly(paths):
    result = CliRunner().invoke(main, ["sources", "add", "mailto:me@example.com", "--yes"])
    assert result.exit_code != 0 and "http" in result.output and "Traceback" not in result.output


HOSTILE_FEED = FEED.replace(
    b"<title>Example Blog</title>", b"<title>Evil&#27;]0;pwned&#7; Blog</title>"
).replace(b"<title>Second</title>", b"<title>Sec&#27;[2Jond</title>")


def _no_escapes(text: str) -> bool:
    return "\x1b" not in text and "\x07" not in text


def test_terminal_escapes_in_a_feed_never_reach_the_terminal(fast_http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(FEED_URL).mock(return_value=httpx.Response(200, content=HOSTILE_FEED))
    added = CliRunner().invoke(main, ["sources", "add", "--rss", FEED_URL, "--yes"])
    assert added.exit_code == 0, added.output
    conn = open_db(fast_http)
    repo = SourcesRepo(conn)
    source_id = repo.find_by_feed_url(FEED_URL)
    assert source_id == "evil-blog"
    record = repo.get(source_id)
    assert record is not None and record.source.name == "Evil Blog"
    repo.record_failure(source_id, "HTTP 500 \x1b]0;owned\x07", now=datetime.now(UTC))
    conn.close()
    tested = CliRunner().invoke(main, ["sources", "test", source_id])
    listed = CliRunner().invoke(main, ["sources", "list"])
    assert "Evil Blog" in added.output and "Second" in tested.output and "HTTP 500" in listed.output
    assert all(_no_escapes(r.output) for r in (added, tested, listed))


def test_scout_report_strips_escapes_from_errors():
    report = ScoutReport(
        run_id="r",
        status="partial",
        sources={"x": SourceStats(error="HTTP 500 \x1b[2J(evil)")},
        new_items=0,
        enrich_error="1 of 1 failed; first: web:1: ValueError: \x1b]0;t\x07",
    )
    out = format_report(report)
    assert _no_escapes(out) and "HTTP 500 (evil)" in out


def test_test_command_explains_a_response_it_cannot_read(fast_http, respx_mock):
    respx_mock.get("https://huggingface.co/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://huggingface.co/api/blog").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    result = CliRunner().invoke(main, ["sources", "test", "hf-blog"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "Error:" in result.output and "JSONDecodeError" in result.output
