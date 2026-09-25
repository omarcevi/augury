from typing import Any, ClassVar

from augury.core.clock import parse_api_datetime
from augury.core.models import FetchResult, FetchState, HfPapersRecipe, RawItem, Signals, Source
from augury.sources.base import AdapterError, map_entries
from augury.sources.http import HttpClient

HF_API = "https://huggingface.co"


def paper_entry_to_raw(source_id: str, entry: dict[str, Any], rank: int) -> RawItem:
    paper = entry["paper"]
    paper_id = paper["id"]
    return RawItem(
        source_id=source_id,
        kind="paper",
        url=f"{HF_API}/papers/{paper_id}",
        title=paper["title"],
        published_at=parse_api_datetime(paper.get("publishedAt")),
        summary=paper.get("summary") or entry.get("summary") or "",
        authors=[
            a["name"] for a in paper.get("authors") or [] if a.get("name") and not a.get("hidden")
        ],
        signals=Signals(
            upvotes=paper.get("upvotes"),
            github_stars=paper.get("githubStars"),
            comments=entry.get("numComments"),
        ),
        arxiv_id=paper_id,
        image_url=entry.get("thumbnail"),
        rank=rank,
    )


class HfPapersAdapter:
    recipe_type: ClassVar[str] = "hf_papers"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        assert isinstance(source.recipe, HfPapersRecipe)
        url = f"{HF_API}/api/daily_papers?sort=trending&limit={source.recipe.limit}"
        data = (await http.get(url)).json()
        if not isinstance(data, list):
            raise AdapterError("expected a JSON list of papers")
        items, skipped = map_entries(source.id, data, paper_entry_to_raw)
        return FetchResult(items=items, state=state, skipped=skipped)
