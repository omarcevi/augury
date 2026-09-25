import pytest
from pydantic import ValidationError

from augury.core.models import RECIPE_ADAPTER, RawItem, RssRecipe, Source


def test_recipe_union_picks_by_type():
    recipe = RECIPE_ADAPTER.validate_python({"type": "rss", "feed_url": "https://x.com/feed"})
    assert isinstance(recipe, RssRecipe)


def test_unknown_recipe_type_is_rejected():
    with pytest.raises(ValidationError):
        RECIPE_ADAPTER.validate_python({"type": "carrier-pigeon"})


def test_source_id_must_be_a_slug():
    with pytest.raises(ValidationError):
        Source(
            id="Not A Slug", name="x", origin="user", recipe=RssRecipe(feed_url="https://x.com/f")
        )


def test_raw_item_defaults():
    item = RawItem(source_id="s", url="https://x.com/a", title="A")
    assert item.kind == "article" and item.summary == "" and item.signals.upvotes is None
