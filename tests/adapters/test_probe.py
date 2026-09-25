import re
from datetime import UTC, datetime

import httpx
import pytest

from augury.sources.http import PoliteClient
from augury.sources.probe import MAX_ALTERNATE_LINKS, candidate_paths, feed_links, probe_url
from tests.adapters.test_rss import FEED
from tests.helpers import FakeTime

ORIGIN = "https://blog.example.com"
NOW = datetime(2026, 9, 25, tzinfo=UTC)
PAGE_WITH_LINK = b"""<html><head>
<link rel="alternate" type="application/rss+xml" title="Posts" href="/posts.xml">
</head><body>hi</body></html>"""
PAGE_WITHOUT_LINK = b"<html><head><title>Blog</title></head><body>hi</body></html>"
TWO_ENTRY_FEED = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    b"<item><title>a</title><link>https://x/a</link></item>"
    b"<item><title>b</title><link>https://x/b</link></item></channel></rss>"
)
STALE_FEED = FEED.replace(b"2026", b"2019")
# FEED without its leading `<?xml ...?>` prolog — a valid XML document some servers send.
FEED_WITHOUT_PROLOG = FEED.split(b"?>", 1)[1].lstrip()


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


def _robots(respx_mock, body="User-agent: *\nAllow: /\n"):
    respx_mock.get(f"{ORIGIN}/robots.txt").mock(return_value=httpx.Response(200, text=body))


def test_feed_links_are_resolved_against_the_page():
    assert feed_links(PAGE_WITH_LINK, f"{ORIGIN}/blog/") == [f"{ORIGIN}/posts.xml"]


def test_candidate_paths_include_origin_and_page_prefix():
    urls = candidate_paths(f"{ORIGIN}/engineering/")
    assert f"{ORIGIN}/feed" in urls and f"{ORIGIN}/engineering/feed" in urls
    assert len(urls) == len(set(urls))


async def test_link_alternate_is_found_without_guessing(http, respx_mock):
    _robots(respx_mock, "User-agent: *\nAllow: /\nSitemap: https://blog.example.com/sitemap.xml\n")
    respx_mock.get(f"{ORIGIN}/").mock(return_value=httpx.Response(200, content=PAGE_WITH_LINK))
    respx_mock.get(f"{ORIGIN}/posts.xml").mock(return_value=httpx.Response(200, content=FEED))
    result = await probe_url(f"{ORIGIN}/", http, now=NOW)
    assert [c.feed_url for c in result.candidates] == [f"{ORIGIN}/posts.xml"]
    assert result.sitemaps == [f"{ORIGIN}/sitemap.xml"]


async def test_common_paths_are_guessed_when_no_link(http, respx_mock):
    _robots(respx_mock)
    respx_mock.get(f"{ORIGIN}/").mock(return_value=httpx.Response(200, content=PAGE_WITHOUT_LINK))
    respx_mock.get(f"{ORIGIN}/feed.xml").mock(return_value=httpx.Response(200, content=FEED))
    respx_mock.route().mock(return_value=httpx.Response(404))  # every other guess
    result = await probe_url(f"{ORIGIN}/", http, now=NOW)
    assert [c.feed_url for c in result.candidates] == [f"{ORIGIN}/feed.xml"]
    assert any("404" in a.outcome for a in result.attempts)


async def test_too_small_and_stale_feeds_are_rejected_with_reasons(http, respx_mock):
    _robots(respx_mock)
    page = (
        b'<html><head><link rel="alternate" type="application/rss+xml" href="/small.xml">'
        b'<link rel="alternate" type="application/atom+xml" href="/old.xml"></head></html>'
    )
    respx_mock.get(f"{ORIGIN}/").mock(return_value=httpx.Response(200, content=page))
    respx_mock.get(f"{ORIGIN}/small.xml").mock(
        return_value=httpx.Response(200, content=TWO_ENTRY_FEED)
    )
    respx_mock.get(f"{ORIGIN}/old.xml").mock(return_value=httpx.Response(200, content=STALE_FEED))
    respx_mock.route().mock(return_value=httpx.Response(404))
    result = await probe_url(f"{ORIGIN}/", http, now=NOW)
    assert result.candidates == []
    outcomes = " | ".join(a.outcome for a in result.attempts)
    assert "only 2 entries" in outcomes and "older than a year" in outcomes


async def test_a_feed_url_is_accepted_directly(http, respx_mock):
    _robots(respx_mock)
    respx_mock.get(f"{ORIGIN}/rss").mock(return_value=httpx.Response(200, content=FEED))
    result = await probe_url(f"{ORIGIN}/rss", http, now=NOW)
    assert [c.title for c in result.candidates] == ["Example Blog"]


async def test_a_feed_with_no_prolog_and_generic_content_type_is_accepted_directly(
    http, respx_mock
):
    _robots(respx_mock)
    respx_mock.get(f"{ORIGIN}/rss").mock(
        return_value=httpx.Response(
            200, content=FEED_WITHOUT_PROLOG, headers={"content-type": "text/plain"}
        )
    )
    result = await probe_url(f"{ORIGIN}/rss", http, now=NOW)
    assert [c.title for c in result.candidates] == ["Example Blog"]


async def test_a_feed_with_xml_content_type_and_leading_whitespace_is_accepted_directly(
    http, respx_mock
):
    _robots(respx_mock)
    respx_mock.get(f"{ORIGIN}/rss").mock(
        return_value=httpx.Response(
            200,
            content=b"   \n" + FEED_WITHOUT_PROLOG,
            headers={"content-type": "application/xml; charset=utf-8"},
        )
    )
    result = await probe_url(f"{ORIGIN}/rss", http, now=NOW)
    assert [c.title for c in result.candidates] == ["Example Blog"]


async def test_alternate_links_are_capped_at_five(http, respx_mock):
    _robots(respx_mock)
    alt_links = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="/alt{i}.xml">' for i in range(1, 9)
    )
    page = f"<html><head>{alt_links}</head></html>".encode()
    respx_mock.get(f"{ORIGIN}/").mock(return_value=httpx.Response(200, content=page))
    alt_route = respx_mock.get(url__regex=re.escape(ORIGIN) + r"/alt\d\.xml$").mock(
        return_value=httpx.Response(404)
    )
    respx_mock.get(f"{ORIGIN}/feed.xml").mock(return_value=httpx.Response(200, content=FEED))
    respx_mock.route().mock(return_value=httpx.Response(404))  # every other guess
    result = await probe_url(f"{ORIGIN}/", http, now=NOW)
    assert alt_route.call_count == MAX_ALTERNATE_LINKS
    assert [c.feed_url for c in result.candidates] == [f"{ORIGIN}/feed.xml"]
