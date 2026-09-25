import httpx
from click.testing import CliRunner

from augury.cli import main
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
