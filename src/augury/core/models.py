from datetime import datetime
from typing import Annotated, Literal
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
