import httpx
import pytest

from augury.agents.normalize import normalize
from augury.core.models import FetchState, RssRecipe, Source
from augury.sources.base import AdapterError
from augury.sources.http import PoliteClient, Response
from augury.sources.rss import RssAdapter, inspect_feed, parse_feed
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


RELATIVE_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><link>/</link>'
    b"<item><title>About</title><link>/about</link></item>"
    b"<item><title>Post</title><link>posts/1</link></item></channel></rss>"
)


def _no_xml_decl_feed(text_title: str) -> bytes:
    return (
        f'<rss version="2.0"><channel><title>t</title><item><title>{text_title}</title>'
        "<link>https://example.com/p</link></item></channel></rss>"
    ).encode()


def test_a_utf8_feed_without_a_content_type_header_is_not_bozo():
    # No XML declaration and no HTTP Content-Type: feedparser's own default (iso-8859-1)
    # would mis-decode this, and it flags every such feed `bozo` (P5.3).
    body = _no_xml_decl_feed("“Quoted” 你好")  # curly quotes + CJK
    resp = Response("https://example.com/feed.xml", 200, httpx.Headers({}), body)
    feed = parse_feed(resp)
    assert feed.bozo == 0
    assert feed.entries[0].title == "“Quoted” 你好"


def test_a_real_content_type_header_is_passed_through_not_overridden():
    # A server's own charset (here iso-8859-1, no XML declaration either) must still be
    # honored -- our default ("application/xml", implying utf-8) must not shadow it.
    body = (
        '<rss version="2.0"><channel><title>t</title><item><title>Caf\xe9</title>'.encode(
            "iso-8859-1"
        )
        + b"<link>https://example.com/p</link></item></channel></rss>"
    )
    headers = httpx.Headers({"content-type": "application/rss+xml; charset=iso-8859-1"})
    resp = Response("https://example.com/feed.xml", 200, headers, body)
    feed = parse_feed(resp)
    assert feed.entries[0].title == "Café"


async def test_relative_entry_links_resolve_against_the_feed(http, respx_mock):
    ids: list[str] = []
    for origin in ("https://a.example", "https://b.example"):
        feed_url = f"{origin}/blog/feed.xml"
        allow_robots(respx_mock, origin)
        respx_mock.get(feed_url).mock(return_value=httpx.Response(200, content=RELATIVE_FEED))
        source = Source(id="s", name="S", origin="user", recipe=RssRecipe(feed_url=feed_url))
        about, post = (await RssAdapter().fetch(source, FetchState(), http)).items
        assert (about.url, post.url) == (f"{origin}/about", f"{origin}/blog/posts/1")
        ids.append(normalize(about).id)
        assert (await inspect_feed(feed_url, http)).homepage == f"{origin}/"
    assert ids[0] != ids[1]  # two blogs' /about pages stay two items
