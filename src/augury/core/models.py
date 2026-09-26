from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

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


Recipe = Annotated[
    HfPapersRecipe | HfBlogRecipe | HfCommunityRecipe | RssRecipe, Field(discriminator="type")
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
