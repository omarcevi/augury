import httpx
import pytest

from augury.core.models import FetchState, RssRecipe, Source
from augury.sources.base import AdapterError
from augury.sources.http import PoliteClient
from augury.sources.rss import RssAdapter, inspect_feed
from tests.helpers import FakeTime, allow_robots

ORIGIN = "https://example.com"
FEED_URL = f"{ORIGIN}/feed.xml"
FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example Blog</title><link>https://example.com/</link>
<item><title>First &amp; best</title><link>https://example.com/p1</link>
  <pubDate>Wed, 24 Sep 2026 10:00:00 GMT</pubDate>
  <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://example.com/p2</link>
  <pubDate>Tue, 23 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Third, undated</title><link>https://example.com/p3</link></item>
</channel></rss>"""
SOURCE = Source(id="example", name="Example", origin="user", recipe=RssRecipe(feed_url=FEED_URL))


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


def _serve(respx_mock, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
    allow_robots(respx_mock, ORIGIN)
    return respx_mock.get(FEED_URL).mock(
        return_value=httpx.Response(status, content=body, headers=headers or {})
    )


async def test_feed_entries_become_raw_items(http, respx_mock):
    _serve(respx_mock, FEED)
    result = await RssAdapter().fetch(SOURCE, FetchState(), http)
    first = result.items[0]
    assert first.title == "First & best" and first.summary == "Hello world"
    assert first.url == "https://example.com/p1" and first.published_at is not None


async def test_entry_without_date_is_kept(http, respx_mock):
    _serve(respx_mock, FEED)
    items = (await RssAdapter().fetch(SOURCE, FetchState(), http)).items
    assert items[2].title == "Third, undated" and items[2].published_at is None


async def test_latin1_feed_decodes_titles(http, respx_mock):
    body = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel>'
        "<title>Caf\xe9</title><item><title>Caf\xe9 cr\xe8me</title>"
        "<link>https://example.com/c</link></item></channel></rss>"
    ).encode("latin-1")
    _serve(respx_mock, body)
    items = (await RssAdapter().fetch(SOURCE, FetchState(), http)).items
    assert items[0].title == "Café crème"


async def test_empty_feed_is_a_failure(http, respx_mock):
    _serve(
        respx_mock,
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>',
    )
    with pytest.raises(AdapterError, match="0 entries"):
        await RssAdapter().fetch(SOURCE, FetchState(), http)


async def test_etag_is_remembered_and_304_means_nothing_new(http, respx_mock):
    route = _serve(respx_mock, FEED, headers={"ETag": '"v1"'})
    first = await RssAdapter().fetch(SOURCE, FetchState(), http)
    assert first.state.etag == '"v1"'
    route.mock(return_value=httpx.Response(304))
    second = await RssAdapter().fetch(SOURCE, first.state, http)
    assert second.not_modified and second.items == [] and second.state.etag == '"v1"'


async def test_inspect_feed_summarizes(http, respx_mock):
    _serve(respx_mock, FEED)
    info = await inspect_feed(FEED_URL, http)
    assert (info.title, info.entries, info.homepage) == ("Example Blog", 3, "https://example.com/")
    assert info.sample_titles == ["First & best", "Second", "Third, undated"]
    assert info.newest is not None and info.newest.day == 24
