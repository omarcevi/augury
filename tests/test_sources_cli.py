import httpx
from click.testing import CliRunner

from augury.cli import main
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
