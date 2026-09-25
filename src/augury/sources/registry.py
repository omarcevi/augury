from augury.core.models import Source
from augury.sources.base import Adapter, AdapterError
from augury.sources.hf_blog import HfBlogAdapter
from augury.sources.hf_community import HfCommunityAdapter
from augury.sources.hf_papers import HfPapersAdapter
from augury.sources.rss import RssAdapter

ADAPTERS: dict[str, Adapter] = {
    a.recipe_type: a
    for a in (HfPapersAdapter(), HfBlogAdapter(), HfCommunityAdapter(), RssAdapter())
}


def adapter_for(source: Source) -> Adapter:
    try:
        return ADAPTERS[source.recipe.type]
    except KeyError:
        raise AdapterError(f"no adapter for recipe type {source.recipe.type!r}") from None
