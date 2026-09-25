from typing import Any, ClassVar

from augury.core.clock import parse_api_datetime
from augury.core.models import FetchResult, FetchState, RawItem, Signals, Source
from augury.sources.base import AdapterError, map_entries
from augury.sources.hf_papers import HF_API
from augury.sources.http import HttpClient


def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else f"{HF_API}{url}"


def blog_entry_to_raw(source_id: str, entry: dict[str, Any], rank: int) -> RawItem:
    # The HF blog APIs carry no summary; Task 13 (enrichment) fills it in later.
    return RawItem(
        source_id=source_id,
        kind="article",
        url=_absolute(entry["url"]) or "",
        title=entry["title"],
        published_at=parse_api_datetime(entry.get("publishedAt")),
        authors=[
            a.get("fullname") or a["name"]
            for a in entry.get("authorsData") or []
            if a.get("fullname") or a.get("name")
        ],
        signals=Signals(upvotes=entry.get("upvotes"), upvotes7d=entry.get("upvotes7d")),
        image_url=_absolute(entry.get("thumbnail")),
        rank=rank,
    )


class HfBlogAdapter:
    recipe_type: ClassVar[str] = "hf_blog"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        data = (await http.get(f"{HF_API}/api/blog")).json()
        entries = data.get("allBlogs") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise AdapterError("expected an object with an 'allBlogs' list")
        items, skipped = map_entries(source.id, entries, blog_entry_to_raw)
        return FetchResult(items=items, state=state, skipped=skipped)
