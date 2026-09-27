"""Sitemap recipes (spec §4.3). The sitemap lists URLs and maybe a <lastmod>, nothing else, so
each URL that is new since the last run gets its page read for og:title, og:description and
article:published_time -- at most max_new_per_run pages per run, newest first. The URLs a run
has handled are kept in FetchState.seen_urls; the first run marks the whole backlog as seen,
so later runs only fetch pages that really are new."""

import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from augury.core.clock import parse_api_datetime
from augury.core.models import (
    MAX_SUMMARY_CHARS,
    FetchResult,
    FetchState,
    RawItem,
    SitemapRecipe,
    Source,
)
from augury.sources.base import AdapterError
from augury.sources.http import HttpClient, HttpError, NetworkError
from augury.sources.rss import plain_text

MAX_CHILD_SITEMAPS = 3  # a sitemap index: only the newest few children are read
MAX_SEEN = 5000  # FetchState.seen_urls cap (JSON in sources.fetch_state_json)
MAX_UNZIPPED = 10_000_000  # the same cap PoliteClient puts on a response
MAX_MATCH_PATH = 512  # recipe patterns only ever see this much of a URL's path
_GZIP_MAGIC = b"\x1f\x8b"
_DTD_MESSAGE = "the sitemap declares a DTD or entities, which sitemaps never need"


class _DtdDeclared(Exception):
    """Raised from inside an expat callback to abort parsing a DTD-bearing sitemap."""


def _refuse(*_args: object) -> None:
    raise _DtdDeclared


def _reject_dtd(data: bytes) -> None:
    """No entity tricks (billion laughs, external entities): sitemaps never need a DTD.
    A byte-level ASCII scan for "<!DOCTYPE"/"<!ENTITY" would miss one hiding in a UTF-16
    (or other non-ASCII) payload, since expat -- like ET.fromstring below -- decodes by
    the BOM or an `encoding=` declaration, not as ASCII. So this runs a real (but
    otherwise inert) expat pass over the same bytes first, with handlers that abort the
    moment a DOCTYPE or an entity declaration appears, whatever the encoding."""
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    try:
        parser.Parse(data, True)
    except _DtdDeclared as e:
        raise AdapterError(_DTD_MESSAGE) from e
    except expat.ExpatError:
        pass  # malformed XML, not a DTD: ET.fromstring below raises the real error


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None


@dataclass(frozen=True)
class ParsedSitemap:
    urls: list[SitemapEntry]  # a <urlset>
    children: list[SitemapEntry]  # a <sitemapindex>


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _gunzip(content: bytes) -> bytes:
    if not content.startswith(_GZIP_MAGIC):
        return content
    try:
        data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(content, MAX_UNZIPPED + 1)
    except zlib.error as e:
        raise AdapterError(f"the sitemap is not valid gzip: {e}") from e
    if len(data) > MAX_UNZIPPED:
        raise AdapterError(f"the sitemap unzips to more than {MAX_UNZIPPED} bytes")
    return data


def _lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00+00:00"
    return parse_api_datetime(value)


def parse_sitemap(content: bytes, base_url: str) -> ParsedSitemap:
    """A <urlset> or a <sitemapindex>. Relative and non-http(s) <loc>s are resolved against
    `base_url` and dropped when they still aren't web addresses. Raises AdapterError."""
    data = _gunzip(content)
    _reject_dtd(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise AdapterError(f"the sitemap is not valid XML: {e}") from e
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise AdapterError(f"not a sitemap (root element <{kind}>)")
    entries: list[SitemapEntry] = []
    for node in root:
        loc = mod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                mod = child.text
        if not loc:
            continue
        url = urljoin(base_url, loc)
        if urlsplit(url).scheme in ("http", "https"):
            entries.append(SitemapEntry(url, _lastmod(mod)))
    if kind == "urlset":
        return ParsedSitemap(urls=entries, children=[])
    return ParsedSitemap(urls=[], children=entries)


def newest_first(entries: list[SitemapEntry]) -> list[SitemapEntry]:
    """By <lastmod>, newest first; undated entries keep their order, after the dated ones."""
    oldest = datetime.min.replace(tzinfo=UTC)
    dated = sorted((e for e in entries if e.lastmod), key=lambda e: e.lastmod or oldest)
    return [*reversed(dated), *(e for e in entries if not e.lastmod)]


def matching(entries: list[SitemapEntry], recipe: SitemapRecipe) -> list[SitemapEntry]:
    include = re.compile(recipe.include_pattern)
    exclude = re.compile(recipe.exclude_pattern) if recipe.exclude_pattern else None
    out: list[SitemapEntry] = []
    for e in entries:
        path = (urlsplit(e.url).path or "/")[:MAX_MATCH_PATH]
        if include.search(path) and not (exclude and exclude.search(path)):
            out.append(e)
    return list({e.url: e for e in out}.values())  # a URL listed twice counts once


@dataclass(frozen=True)
class PageMeta:
    title: str
    description: str
    published_at: datetime | None
    image_url: str | None


def _meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        for attr in ("property", "name"):
            tag = soup.find("meta", attrs={attr: key})
            content = tag.get("content") if isinstance(tag, Tag) else None
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def page_meta(html: str, base_url: str) -> PageMeta:
    # Text, not bytes: on bytes without a charset, bs4 can guess UTF-16 and garble the page.
    soup = BeautifulSoup(html, "html.parser")
    title = _meta(soup, "og:title", "twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string
    published = _meta(soup, "article:published_time", "og:published_time", "date")
    if not published and isinstance(time := soup.find("time"), Tag):
        published = str(time.get("datetime") or "")
    image = _meta(soup, "og:image")
    return PageMeta(
        title=plain_text(title),
        description=plain_text(_meta(soup, "og:description", "description"))[:MAX_SUMMARY_CHARS],
        published_at=parse_api_datetime(published),
        image_url=urljoin(base_url, image) if image else None,
    )


async def read_entries(recipe: SitemapRecipe, state: FetchState, http: HttpClient):
    """(entries, response headers) for the recipe's sitemap, or (None, None) on a 304."""
    resp = await http.get(recipe.sitemap_url, etag=state.etag, last_modified=state.last_modified)
    if resp.not_modified:
        return None, None
    parsed = parse_sitemap(resp.content, resp.url)
    entries = parsed.urls
    for child in newest_first(parsed.children)[:MAX_CHILD_SITEMAPS]:
        try:
            child_resp = await http.get(child.url)
            entries += parse_sitemap(child_resp.content, child_resp.url).urls
        except HttpError, AdapterError:
            continue  # one unreadable child doesn't sink the others
    return entries, resp.headers


class SitemapAdapter:
    recipe_type: ClassVar[str] = "sitemap"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        recipe = source.recipe
        assert isinstance(recipe, SitemapRecipe)
        entries, headers = await read_entries(recipe, state, http)
        if entries is None or headers is None:
            return FetchResult(items=[], state=state, not_modified=True)
        wanted = newest_first(matching(entries, recipe))
        if not wanted:
            raise AdapterError(
                f"the sitemap has 0 URLs matching {recipe.include_pattern!r} "
                "(the site or its sitemap may have changed)"
            )
        seen = set(state.seen_urls)
        new = [e for e in wanted if e.url not in seen]
        attempts = new[: recipe.max_new_per_run]
        items: list[RawItem] = []
        skipped = 0
        retry_later: set[str] = set()
        for rank, entry in enumerate(attempts, start=1):
            try:
                page = await http.get(entry.url)
                meta = page_meta(page.text(), page.url)
            except NetworkError:
                retry_later.add(entry.url)  # try again next run
                skipped += 1
                continue
            except HttpError:
                skipped += 1  # a 404 or a robots block: don't keep asking
                continue
            if not meta.title:
                skipped += 1
                continue
            items.append(
                RawItem(
                    source_id=source.id,
                    url=entry.url,
                    title=meta.title,
                    published_at=meta.published_at or entry.lastmod,
                    summary=meta.description,
                    image_url=meta.image_url,
                    rank=rank,
                )
            )
        if attempts and not items:
            raise AdapterError(f"none of the {len(attempts)} new pages could be read")
        # Everything listed now counts as handled, except pages that failed on the network.
        handled = [e.url for e in wanted if e.url not in retry_later][:MAX_SEEN]
        new_state = FetchState(
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
            seen_urls=handled,
        )
        return FetchResult(items=items, state=new_state, skipped=skipped)
