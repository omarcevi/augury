"""html_listing recipes (spec §4.3): a blog's index page read with CSS selectors. Deterministic
at run time. Selectors that match nothing are a failure (spec §4.4): the page changed shape."""

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from augury.core.clock import parse_api_datetime
from augury.core.models import FetchResult, FetchState, HtmlListingRecipe, RawItem, Source
from augury.sources.base import AdapterError, map_entries
from augury.sources.http import HttpClient
from augury.sources.rss import plain_text

MAX_ITEMS = 50  # a listing page's first 50 matches; older posts were seen on earlier runs


def _date(node: Tag, recipe: HtmlListingRecipe) -> datetime | None:
    if not recipe.date_selector or not isinstance(
        found := node.select_one(recipe.date_selector), Tag
    ):
        return None
    candidates = [str(found.get("datetime") or ""), found.get_text(" ", strip=True)]
    for value in (c.strip() for c in candidates if c.strip()):
        if recipe.date_format:
            try:
                dt = datetime.strptime(value, recipe.date_format)
            except ValueError:
                pass
            else:
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        if dt := parse_api_datetime(value):
            return dt
    return None


def _href(node: Tag, selector: str) -> str | None:
    # The item itself may be the link (e.g. item_selector "a.post-card", link_selector ":scope").
    found = node if node.name == "a" and selector == ":scope" else node.select_one(selector)
    if not isinstance(found, Tag):
        return None
    if found.name != "a" and isinstance(inner := found.find("a"), Tag):
        found = inner
    href = found.get("href")
    return href.strip() if isinstance(href, str) and href.strip() else None


def entry_mapper(recipe: HtmlListingRecipe, base_url: str):
    def to_raw(source_id: str, node: Any, rank: int) -> RawItem:
        href = _href(node, recipe.link_selector)
        title_node = node.select_one(recipe.title_selector)
        if href is None or not isinstance(title_node, Tag):
            raise KeyError("link or title")
        url = urljoin(base_url, href)
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"not a web link: {href!r}")
        title = plain_text(title_node.get_text(" "))
        if not title:
            raise ValueError("empty title")
        return RawItem(
            source_id=source_id, url=url, title=title, published_at=_date(node, recipe), rank=rank
        )

    return to_raw


def parse_listing(html: str, base_url: str, recipe: HtmlListingRecipe, source_id: str):
    """(items, skipped). Raises AdapterError when item_selector matches nothing, or when no
    match has both a link and a title (the other selectors match nothing)."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(recipe.item_selector, limit=MAX_ITEMS)
    if not nodes:
        raise AdapterError(f"item_selector {recipe.item_selector!r} matched 0 elements")
    try:
        return map_entries(source_id, nodes, entry_mapper(recipe, base_url))
    except AdapterError as e:
        raise AdapterError(
            f"{e}: link_selector {recipe.link_selector!r} or title_selector"
            f" {recipe.title_selector!r} matched nothing inside the items"
        ) from e


class HtmlListingAdapter:
    recipe_type: ClassVar[str] = "html_listing"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        recipe = source.recipe
        assert isinstance(recipe, HtmlListingRecipe)
        resp = await http.get(
            recipe.listing_url, etag=state.etag, last_modified=state.last_modified
        )
        if resp.not_modified:
            return FetchResult(items=[], state=state, not_modified=True)
        items, skipped = parse_listing(resp.text(), resp.url, recipe, source.id)
        new_state = FetchState(
            etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified")
        )
        return FetchResult(items=items, state=new_state, skipped=skipped)
