"""What discovery passes around: the recipe arguments a model writes, tested samples, and the
candidates the user confirms."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from augury.core.models import HtmlListingRecipe, RssRecipe, SitemapRecipe
from augury.core.text import strip_control_chars

# The recipe types discovery may propose, in the order it prefers them (spec §4.3).
UserRecipe = Annotated[RssRecipe | SitemapRecipe | HtmlListingRecipe, Field(discriminator="type")]
USER_RECIPE: TypeAdapter[UserRecipe] = TypeAdapter(UserRecipe)
MAX_CANDIDATES = 5
SAMPLES = 3
MAX_URL_CHARS = 300  # longer URLs in tool results are dropped: one page can't flood a prompt


class RecipeArg(BaseModel):
    """A recipe as a tool argument: one flat object (models fill these in more reliably than
    a union). Only the fields of the chosen `type` are read."""

    type: Literal["rss", "sitemap", "html_listing"]
    feed_url: str | None = Field(default=None, description="rss: the RSS or Atom feed URL")
    sitemap_url: str | None = Field(default=None, description="sitemap: the sitemap URL")
    include_pattern: str | None = Field(
        default=None, description="sitemap: a regex that matches the path of every post URL"
    )
    exclude_pattern: str | None = Field(default=None, description="sitemap: optional regex")
    listing_url: str | None = Field(default=None, description="html_listing: the index page")
    item_selector: str | None = Field(default=None, description="html_listing: one post")
    link_selector: str | None = Field(default=None, description="html_listing: link, in item")
    title_selector: str | None = Field(default=None, description="html_listing: title, in item")
    date_selector: str | None = Field(default=None, description="html_listing: optional date")
    date_format: str | None = Field(default=None, description="html_listing: strptime format")

    def to_recipe(self) -> UserRecipe:
        """Raises pydantic's ValidationError, with the reason, for an unusable recipe."""
        data = {k: v for k, v in self.model_dump().items() if v is not None}
        return USER_RECIPE.validate_python(data)


def recipe_hash(recipe: UserRecipe) -> str:
    """Stable for equal recipes: submit_candidates only accepts hashes test_recipe returned."""
    canonical = json.dumps(recipe.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class SampleItem(BaseModel):
    title: str
    url: str
    published_at: str | None = None


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str = ""


def _clean(text: str, limit: int) -> str:
    return " ".join(strip_control_chars(text).split())[:limit]


class Candidate(BaseModel):
    """A tested source the user can confirm. sample_items come from our own test_recipe run,
    never from what the model claims."""

    name: str
    homepage: str
    recipe: UserRecipe
    recipe_hash: str
    sample_items: list[SampleItem]
    confidence: float = Field(default=0.5, ge=0, le=1)
    note: str = ""
    duplicate_of: str | None = None  # the id of a source that already fetches this URL

    @property
    def duplicate(self) -> bool:
        return self.duplicate_of is not None

    @classmethod
    def build(
        cls,
        *,
        name: str,
        homepage: str,
        recipe: UserRecipe,
        samples: list[SampleItem],
        confidence: float,
        note: str,
    ) -> Candidate:
        return cls(
            name=_clean(name, 80) or "New source",
            homepage=_clean(homepage, 300),
            recipe=recipe,
            recipe_hash=recipe_hash(recipe),
            sample_items=samples[:SAMPLES],
            confidence=min(1.0, max(0.0, confidence)),
            note=_clean(note, 200),
        )


def primary_url(recipe: UserRecipe) -> str:
    match recipe:
        case RssRecipe():
            return recipe.feed_url
        case SitemapRecipe():
            return recipe.sitemap_url
        case HtmlListingRecipe():
            return recipe.listing_url
