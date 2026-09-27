import gzip
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from augury.core.models import RECIPE_ADAPTER, FetchState, SitemapRecipe, Source
from augury.sources.base import AdapterError
from augury.sources.http import PoliteClient, ResponseTooLarge
from augury.sources.registry import ADAPTERS
from augury.sources.sitemap import (
    SitemapAdapter,
    SitemapEntry,
    matching,
    newest_first,
    page_meta,
    parse_sitemap,
)
from tests.helpers import FakeTime, allow_robots

PAGES = Path(__file__).parent.parent / "fixtures" / "pages"
ORIGIN = "https://eng.example.com"
SITEMAP_URL = f"{ORIGIN}/sitemap.xml"
SITEMAP = (PAGES / "sitemap_blog.xml").read_bytes()
POST = (PAGES / "blog_post_meta.html").read_bytes()
RECIPE = SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=r"^/blog/\d{4}/")
SOURCE = Source(id="eng", name="Eng", origin="user", recipe=RECIPE)


def _post(title: str) -> bytes:
    return POST.replace(b"Serving at scale", title.encode())


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


def _serve(respx_mock, sitemap: bytes = SITEMAP, **headers: str):
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, content=sitemap, headers=headers)
    )
    for slug in ("serving-at-scale", "speculative-decoding", "relative-link-post", "old-post"):
        year = "2025" if slug == "old-post" else "2026"
        respx_mock.get(f"{ORIGIN}/blog/{year}/{slug}").mock(
            return_value=httpx.Response(200, content=_post(slug.replace("-", " ")))
        )
    return route


def test_the_recipe_round_trips_and_rejects_a_bad_pattern():
    assert RECIPE_ADAPTER.validate_json(RECIPE.model_dump_json()) == RECIPE
    with pytest.raises(ValidationError, match="regular expression"):
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern="(")
    with pytest.raises(ValidationError):
        SitemapRecipe(sitemap_url="ftp://x/sitemap.xml", include_pattern="/")


def test_parse_resolves_relative_locs_and_drops_non_web_ones():
    parsed = parse_sitemap(SITEMAP, SITEMAP_URL)
    urls = [e.url for e in parsed.urls]
    assert f"{ORIGIN}/blog/2026/relative-link-post" in urls
    assert not any(u.startswith("mailto:") for u in urls) and len(urls) == 7
    assert parsed.children == []


def test_parse_reads_a_sitemap_index():
    parsed = parse_sitemap((PAGES / "sitemap_index.xml").read_bytes(), SITEMAP_URL)
    assert newest_first(parsed.children)[0].url == f"{ORIGIN}/sitemap-posts.xml"


def test_parse_accepts_a_gzipped_sitemap():
    assert len(parse_sitemap(gzip.compress(SITEMAP), SITEMAP_URL).urls) == 7


@pytest.mark.parametrize(
    "body",
    [
        b"<urlset><url><loc>https://x/a</loc>",  # truncated
        b"<html><body>not a sitemap</body></html>",
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><urlset>&lol;</urlset>',
        b"\x1f\x8bnot really gzip",
    ],
)
def test_malformed_or_hostile_sitemaps_are_adapter_errors(body):
    with pytest.raises(AdapterError):
        parse_sitemap(body, SITEMAP_URL)


def test_a_utf16_billion_laughs_sitemap_is_refused_like_the_ascii_one():
    # Expat decodes by BOM, not as ASCII: an entity-expansion bomb hiding in a UTF-16
    # payload must be caught exactly like the ASCII case in the parametrized test above.
    payload = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<urlset>&lol2;</urlset>"
    )
    with pytest.raises(AdapterError, match="DTD or entities"):
        parse_sitemap(payload.encode("utf-16"), SITEMAP_URL)


def test_a_utf16_doctype_only_sitemap_is_refused_too():
    # No entities at all, just a DOCTYPE: refused outright, same as the ASCII case.
    payload = (
        '<?xml version="1.0"?><!DOCTYPE urlset>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://eng.example.com/a</loc></url></urlset>"
    )
    with pytest.raises(AdapterError, match="DTD or entities"):
        parse_sitemap(payload.encode("utf-16"), SITEMAP_URL)


def test_a_normal_utf16_sitemap_without_a_dtd_still_parses():
    # The DTD guard must not reject a harmless sitemap just because it's not ASCII.
    text = SITEMAP.decode("utf-8").replace('<?xml version="1.0" encoding="UTF-8"?>', "")
    assert len(parse_sitemap(text.encode("utf-16"), SITEMAP_URL).urls) == 7


def test_matching_filters_by_path_and_orders_newest_first():
    entries = parse_sitemap(SITEMAP, SITEMAP_URL).urls
    recipe = RECIPE.model_copy(update={"exclude_pattern": "old"})
    urls = [e.url.removeprefix(ORIGIN) for e in newest_first(matching(entries, recipe))]
    assert urls == [
        "/blog/2026/serving-at-scale",
        "/blog/2026/speculative-decoding",
        "/blog/2026/relative-link-post",
    ]


def test_page_meta_reads_open_graph_tags_as_plain_text():
    meta = page_meta(POST.decode(), f"{ORIGIN}/blog/2026/serving-at-scale")
    assert meta.title == "Serving at scale"
    assert meta.description == "How we serve large models to millions of users."
    assert meta.published_at is not None and meta.published_at.year == 2026
    assert meta.image_url == f"{ORIGIN}/img/serving.png"


def test_page_meta_falls_back_to_the_title_tag():
    meta = page_meta("<html><head><title> Plain \x1b[31mtitle</title></head></html>", ORIGIN)
    assert meta.title == "Plain title" and meta.description == "" and meta.published_at is None


async def test_first_run_fetches_the_newest_pages_and_marks_the_backlog_seen(http, respx_mock):
    _serve(respx_mock, etag='"s1"')
    recipe = RECIPE.model_copy(update={"max_new_per_run": 2})
    source = SOURCE.model_copy(update={"recipe": recipe})
    result = await SitemapAdapter().fetch(source, FetchState(), http)
    assert [i.title for i in result.items] == ["serving at scale", "speculative decoding"]
    assert result.items[0].summary.startswith("How we serve") and result.state.etag == '"s1"'
    # all 4 matching URLs are handled, so the next run only reads pages added after this one
    assert len(result.state.seen_urls) == 4


async def test_a_later_run_reads_only_new_urls(http, respx_mock):
    _serve(respx_mock)
    first = await SitemapAdapter().fetch(SOURCE, FetchState(), http)
    assert len(first.items) == 4
    grown = SITEMAP.replace(
        b"</urlset>",
        b"<url><loc>https://eng.example.com/blog/2026/brand-new</loc></url></urlset>",
    )
    respx_mock.get(SITEMAP_URL).mock(return_value=httpx.Response(200, content=grown))
    respx_mock.get(f"{ORIGIN}/blog/2026/brand-new").mock(
        return_value=httpx.Response(200, content=_post("Brand new"))
    )
    second = await SitemapAdapter().fetch(SOURCE, first.state, http)
    assert [i.title for i in second.items] == ["Brand new"]


async def test_nothing_new_is_a_success_with_no_items(http, respx_mock):
    _serve(respx_mock)
    first = await SitemapAdapter().fetch(SOURCE, FetchState(), http)
    again = await SitemapAdapter().fetch(SOURCE, first.state, http)
    assert again.items == [] and again.state.seen_urls == first.state.seen_urls


async def test_304_means_nothing_new(http, respx_mock):
    route = _serve(respx_mock, etag='"s1"')
    first = await SitemapAdapter().fetch(SOURCE, FetchState(), http)
    route.mock(return_value=httpx.Response(304))
    second = await SitemapAdapter().fetch(SOURCE, first.state, http)
    assert second.not_modified and second.state == first.state


async def test_no_matching_url_is_a_failure(http, respx_mock):
    _serve(respx_mock)
    source = SOURCE.model_copy(
        update={"recipe": RECIPE.model_copy(update={"include_pattern": "^/news/"})}
    )
    with pytest.raises(AdapterError, match="0 URLs matching"):
        await SitemapAdapter().fetch(source, FetchState(), http)


async def test_a_missing_page_is_skipped_not_fatal(http, respx_mock):
    _serve(respx_mock)
    respx_mock.get(f"{ORIGIN}/blog/2026/speculative-decoding").mock(
        return_value=httpx.Response(404)
    )
    result = await SitemapAdapter().fetch(SOURCE, FetchState(), http)
    assert len(result.items) == 3 and result.skipped == 1


async def test_every_page_failing_is_a_failure(http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(SITEMAP_URL).mock(return_value=httpx.Response(200, content=SITEMAP))
    respx_mock.get(url__regex=rf"{ORIGIN}/blog/.*").mock(return_value=httpx.Response(410))
    with pytest.raises(AdapterError, match="none of the 4 new pages"):
        await SitemapAdapter().fetch(SOURCE, FetchState(), http)


async def test_an_index_is_followed_to_its_newest_child(http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, content=(PAGES / "sitemap_index.xml").read_bytes())
    )
    respx_mock.get(f"{ORIGIN}/sitemap-posts.xml").mock(
        return_value=httpx.Response(200, content=SITEMAP)
    )
    respx_mock.get(f"{ORIGIN}/sitemap-pages.xml").mock(return_value=httpx.Response(500))
    respx_mock.get(url__regex=rf"{ORIGIN}/blog/.*").mock(
        return_value=httpx.Response(200, content=POST)
    )
    result = await SitemapAdapter().fetch(SOURCE, FetchState(), http)
    assert len(result.items) == 4


async def test_an_oversized_sitemap_is_refused(respx_mock):
    t = FakeTime()
    from augury.core.config import HttpConfig

    async with PoliteClient(HttpConfig(max_bytes=1000), sleep=t.sleep, clock=t.clock) as http:
        allow_robots(respx_mock, ORIGIN)
        respx_mock.get(SITEMAP_URL).mock(return_value=httpx.Response(200, content=b"x" * 5000))
        with pytest.raises(ResponseTooLarge):
            await SitemapAdapter().fetch(SOURCE, FetchState(), http)


def test_the_registry_knows_sitemaps():
    assert isinstance(ADAPTERS["sitemap"], SitemapAdapter)


async def test_seen_urls_are_capped_for_huge_sitemaps(http, respx_mock):
    from augury.sources.sitemap import MAX_SEEN

    urls = "".join(f"<url><loc>{ORIGIN}/blog/2026/p{i}</loc></url>" for i in range(MAX_SEEN + 50))
    big = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=big))
    respx_mock.get(url__regex=rf"{ORIGIN}/blog/2026/p\d+").mock(
        return_value=httpx.Response(200, content=POST)
    )
    recipe = RECIPE.model_copy(update={"max_new_per_run": 2})
    result = await SitemapAdapter().fetch(
        SOURCE.model_copy(update={"recipe": recipe}), FetchState(), http
    )
    assert len(result.items) == 2 and len(result.state.seen_urls) == MAX_SEEN


def test_patterns_only_see_the_first_512_characters_of_a_path():
    long = SitemapEntry(f"{ORIGIN}/blog/2026/" + "a" * 600 + "-end", None)
    assert matching([long], RECIPE) == [long]
    tail = SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=r"-end$")
    assert matching([long], tail) == []
