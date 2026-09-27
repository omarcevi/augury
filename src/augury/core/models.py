import re
import re._parser as sre_parser
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import soupsieve
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter

MAX_SUMMARY_CHARS = 1000
Kind = Literal["paper", "article"]


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {value!r}")
    return value


# A plain str (not pydantic's HttpUrl), so callers and pyright can pass ordinary strings.
HttpUrlStr = Annotated[str, AfterValidator(_http_url)]


class HfPapersRecipe(BaseModel):
    type: Literal["hf_papers"] = "hf_papers"
    recipe_version: Literal[1] = 1
    limit: int = Field(default=50, ge=1, le=100)


class HfBlogRecipe(BaseModel):
    type: Literal["hf_blog"] = "hf_blog"
    recipe_version: Literal[1] = 1


class HfCommunityRecipe(BaseModel):
    type: Literal["hf_community"] = "hf_community"
    recipe_version: Literal[1] = 1
    limit: int = Field(default=20, ge=1, le=100)


class RssRecipe(BaseModel):
    type: Literal["rss"] = "rss"
    recipe_version: Literal[1] = 1
    feed_url: HttpUrlStr


MAX_PATTERN_CHARS = 200
_REPEATS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})


def _subpatterns(av: object) -> Iterator[Any]:
    """The parsed subpatterns inside one node's arguments (groups, branches, lookarounds)."""
    if isinstance(av, sre_parser.SubPattern):
        yield av
    elif isinstance(av, tuple | list):
        for part in av:
            yield from _subpatterns(part)


def _nested_repeat(tree: Any, inside: bool = False) -> bool:
    r"""True when a repeat that can run more than once ({2,}, +, *) contains another repeat of
    variable length: (a+)+, (\w+\s?)* or (a*)*, the shape that backtracks exponentially."""
    for op, av in tree:
        if str(op) in _REPEATS:
            low, high, body = av
            if inside and low != high:
                return True
            if _nested_repeat(body, inside or high > 1):
                return True
        elif any(_nested_repeat(sub, inside) for sub in _subpatterns(av)):
            return True
    return False


def _regex(value: str) -> str:
    """A pattern for URL paths. Nested repeats are refused: a model writes these patterns and
    any site supplies the paths, and one catastrophic backtrack would freeze the event loop.
    Overlapping alternatives in a repeat, like (a|a)+, can still backtrack; they are rare."""
    if len(value) > MAX_PATTERN_CHARS:
        raise ValueError(f"a regular expression can be at most {MAX_PATTERN_CHARS} characters")
    try:
        re.compile(value)
        tree = sre_parser.parse(value)
    except re.error as e:
        raise ValueError(f"not a valid regular expression: {e}") from e
    if _nested_repeat(tree):
        raise ValueError(
            "a regular expression can't repeat a group that holds another repeat, like (a+)+ "
            "or ([a-z0-9-]+/?)+: it can take forever to match; write it without one"
        )
    return value


Regex = Annotated[str, AfterValidator(_regex)]


class SitemapRecipe(BaseModel):
    """A sitemap's URLs whose path matches include_pattern (spec §4.3). Sitemaps carry no
    titles, so each new URL's page is read for og:title / og:description / published time."""

    type: Literal["sitemap"] = "sitemap"
    recipe_version: Literal[1] = 1
    sitemap_url: HttpUrlStr
    include_pattern: Regex
    exclude_pattern: Regex | None = None
    max_new_per_run: int = Field(default=20, ge=1, le=100)


def _selector(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("a CSS selector can't be empty")
    try:
        soupsieve.compile(value)
    except soupsieve.SelectorSyntaxError as e:
        raise ValueError(f"not a valid CSS selector: {e}") from e
    return value


Selector = Annotated[str, AfterValidator(_selector)]


class HtmlListingRecipe(BaseModel):
    """A listing page read with CSS selectors (spec §4.3): item_selector finds each post, and
    the others run inside it. The discovery agent picks them; test_recipe proves they work."""

    type: Literal["html_listing"] = "html_listing"
    recipe_version: Literal[1] = 1
    listing_url: HttpUrlStr
    item_selector: Selector
    link_selector: Selector
    title_selector: Selector
    date_selector: Selector | None = None
    date_format: str | None = None  # strptime format; ISO 8601 is tried without one


Recipe = Annotated[
    HfPapersRecipe
    | HfBlogRecipe
    | HfCommunityRecipe
    | RssRecipe
    | SitemapRecipe
    | HtmlListingRecipe,
    Field(discriminator="type"),
]
RECIPE_ADAPTER: TypeAdapter[Recipe] = TypeAdapter(Recipe)


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str
    homepage: str = ""
    origin: Literal["builtin", "user"]
    recipe: Recipe
    enabled: bool = True
    added_via: Literal["builtin", "url_probe", "discovery", "manual"] = "manual"
    trust: Literal["publisher", "community"] = "publisher"


class Signals(BaseModel):
    upvotes: int | None = None
    upvotes7d: int | None = None
    github_stars: int | None = None
    comments: int | None = None


class RawItem(BaseModel):
    source_id: str
    url: str
    title: str
    kind: Kind = "article"
    published_at: datetime | None = None
    summary: str = ""
    authors: list[str] = Field(default_factory=list)
    signals: Signals = Field(default_factory=Signals)
    arxiv_id: str | None = None
    image_url: str | None = None
    rank: int | None = None


class FetchState(BaseModel):
    etag: str | None = None
    last_modified: str | None = None
    # sitemap: the matching URLs already handled, so only new ones cost a page fetch
    seen_urls: list[str] = Field(default_factory=list)


class FetchResult(BaseModel):
    items: list[RawItem]
    state: FetchState
    skipped: int = 0
    not_modified: bool = False


class Item(BaseModel):
    id: str
    source_id: str
    kind: Kind
    title: str
    url: str
    canonical_url: str
    authors: list[str]
    published_at: datetime | None
    summary: str
    arxiv_id: str | None
    image_url: str | None
    first_seen: datetime
    last_seen: datetime
    times_seen: int
    content_hash: str
    is_old: bool


OLD_AFTER = timedelta(days=90)


def is_old(published_at: datetime | None, first_seen: datetime) -> bool:
    return published_at is not None and first_seen - published_at > OLD_AFTER


class NormalizedItem(BaseModel):
    id: str
    source_id: str
    kind: Kind
    title: str
    url: str
    canonical_url: str
    authors: list[str]
    published_at: datetime | None
    summary: str
    arxiv_id: str | None
    image_url: str | None
    content_hash: str
    signals: Signals
    rank: int | None


class Content(BaseModel):
    item_id: str
    status: Literal["ok", "failed", "paywalled"]
    body_md: str = ""
    extractor: str
    extractor_version: int
    word_count: int = 0
    error: str | None = None
    fetched_at: datetime


TRIAGE_FLAGS = ("promo", "thin", "off_topic")
HIDING_FLAGS = frozenset({"promo", "thin"})  # left out of the Top view (spec §5.4)
TRIAGE_FAILED = "triage_failed"


class TriageResult(BaseModel):
    item_id: str
    relevance: int | None = Field(default=None, ge=0, le=10)  # None: triage failed
    why_read: str = ""
    tags: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # TRIAGE_FLAGS, or TRIAGE_FAILED

    @classmethod
    def failed(cls, item_id: str) -> Self:
        return cls(item_id=item_id, flags=[TRIAGE_FAILED])
