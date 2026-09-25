from typing import ClassVar

from augury.core.models import FetchResult, FetchState, HfCommunityRecipe, Source
from augury.sources.base import AdapterError, map_entries
from augury.sources.hf_blog import blog_entry_to_raw
from augury.sources.hf_papers import HF_API
from augury.sources.http import HttpClient


class HfCommunityAdapter:
    recipe_type: ClassVar[str] = "hf_community"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        assert isinstance(source.recipe, HfCommunityRecipe)
        data = (await http.get(f"{HF_API}/api/blog/community?sort=trending")).json()
        posts = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(posts, list):
            raise AdapterError("expected an object with a 'posts' list")
        items, skipped = map_entries(source.id, posts[: source.recipe.limit], blog_entry_to_raw)
        return FetchResult(items=items, state=state, skipped=skipped)
