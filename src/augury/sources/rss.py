import warnings
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import feedparser
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from pydantic import BaseModel

from augury.core.models import (
    MAX_SUMMARY_CHARS,
    FetchResult,
    FetchState,
    RawItem,
    RssRecipe,
    Source,
)
from augury.core.text import strip_control_chars
from augury.sources.base import AdapterError, map_entries
from augury.sources.http import HttpClient, Response


def plain_text(fragment: str) -> str:
    # feedparser's loose parser turns "&#27;" into a raw ESC, so strip here, not only later.
    if "<" in fragment:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
            fragment = BeautifulSoup(fragment, "html.parser").get_text(" ")
    return " ".join(strip_control_chars(fragment).split())


def parse_feed(resp: Response) -> Any:
    # Bytes, so feedparser honours the XML encoding; the URL resolves relative links.
    # Without a content-type, feedparser assumes iso-8859-1 (the old HTTP/XML default) and
    # marks the feed `bozo` -- most feeds have no XML encoding declaration either, so a
    # UTF-8 feed with curly quotes or CJK text gets flagged, or worse, mis-decoded. The
    # real header (when the server sent one) always wins over this default.
    content_type = resp.headers.get("content-type") or "application/xml"
    return feedparser.parse(
        resp.content,
        response_headers={"content-location": resp.url, "content-type": content_type},
    )


def entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        if value := entry.get(key):
            return datetime(*value[:6], tzinfo=UTC)
    return None


def feed_entry_to_raw(source_id: str, entry: Any, rank: int) -> RawItem:
    authors = [a["name"] for a in entry.get("authors", []) if a.get("name")]
    return RawItem(
        source_id=source_id,
        url=entry["link"],
        title=plain_text(entry["title"]),
        published_at=entry_datetime(entry),
        summary=plain_text(entry.get("summary", ""))[:MAX_SUMMARY_CHARS],
        authors=authors,
        rank=rank,
    )


class RssAdapter:
    recipe_type: ClassVar[str] = "rss"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        assert isinstance(source.recipe, RssRecipe)
        resp = await http.get(
            source.recipe.feed_url, etag=state.etag, last_modified=state.last_modified
        )
        if resp.not_modified:
            return FetchResult(items=[], state=state, not_modified=True)
        feed = parse_feed(resp)
        items, skipped = map_entries(source.id, list(feed.entries), feed_entry_to_raw)
        new_state = FetchState(
            etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified")
        )
        return FetchResult(items=items, state=new_state, skipped=skipped)


class FeedInfo(BaseModel):
    feed_url: str
    title: str
    homepage: str
    sample_titles: list[str]
    entries: int
    newest: datetime | None


async def inspect_feed(url: str, http: HttpClient) -> FeedInfo:
    resp = await http.get(url)
    feed = parse_feed(resp)
    usable = [e for e in feed.entries if e.get("title") and e.get("link")]
    if not usable:
        raise AdapterError(f"{url} is not a feed with entries")
    parts = urlsplit(resp.url)
    dates = [d for e in usable if (d := entry_datetime(e))]
    return FeedInfo(
        feed_url=resp.url,
        title=plain_text(feed.feed.get("title") or parts.hostname or url),
        homepage=feed.feed.get("link") or f"{parts.scheme}://{parts.netloc}/",
        sample_titles=[plain_text(e["title"]) for e in usable[:3]],
        entries=len(usable),
        newest=max(dates) if dates else None,
    )
