import pytest
from pydantic import ValidationError

from augury.core.models import RECIPE_ADAPTER, RawItem, RssRecipe, SitemapRecipe, Source


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


SITEMAP_URL = "https://x.example.com/sitemap.xml"


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+",
        r"(\w+\s?)*",
        r"(a*)*",
        r"^/\d{4}/\d{2}/([a-z0-9-]+/?)+$",  # 30 minutes on a 35-character slug
        r"(?:x[a-z]+){2,}",
        r"((?:a{1,3}){2})+",
        r"^/blog/(?=(a+)+)",
    ],
)
def test_sitemap_patterns_that_nest_repeats_are_refused(pattern):
    with pytest.raises(ValidationError, match="repeat a group that holds another repeat"):
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=pattern)
    with pytest.raises(ValidationError, match="repeat a group that holds another repeat"):
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern="^/", exclude_pattern=pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^/blog/\d{4}/",
        r"^/(en/)?blog/[\w-]+/?$",
        r"^/\d{4}/\d{2}/[a-z0-9-]+",
        r"^/(?:news|blog)/(\d{4}/)?[^/]+$",
        r"^/p/[\w-]+(?:\.html)?$",
        r"^/posts/(?!tag/)[^/]+$",
        r"^/(\d{4}/){2}",  # a fixed repeat inside a repeat is linear
    ],
)
def test_ordinary_sitemap_patterns_are_accepted(pattern):
    assert (
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=pattern).include_pattern == pattern
    )


def test_a_sitemap_pattern_has_a_length_cap():
    with pytest.raises(ValidationError, match="at most 200 characters"):
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern="x" * 201)


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        (r"(a|a)+$", "alternatives"),  # 4.6 s on a 26-character path
        (r"(?:x|y)+z", "alternatives"),  # sre folds it into [xy]: caught in the source
        (r"(?:ab|cd)+$", "alternatives"),
        (r"((a|b)c){2,}", "alternatives"),
        ("(?x)(a#)\n|a)+$", "alternatives"),  # a comment hides it from the source: the tree
        (r".*/.*/.*", "open-ended repeats"),
        (r"^/blog/.*/.*/.*$x", "open-ended repeats"),  # ~0.1 s per 512-character path
        (r".{0,500}/.{0,500}/.{0,500}", "open-ended repeats"),
    ],
)
def test_sitemap_patterns_that_backtrack_badly_are_refused(pattern, reason):
    with pytest.raises(ValidationError, match=reason):
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^/blog/[^/]+/?$",
        r"/posts/\d{4}/",
        r"^/(?:en|fr|de-DE)/blog/",  # a locale prefix: alternatives, but not repeated
        r"^/(en/|fr/)?blog/[\w-]+/?$",
        r"^/p/[\w-]+(?:\.html)?$",
        r"(\d{4}/)+",
        r"^/blog/.*",
        r"^/[^/]+/[^/]+/?$",
        r"^/blog/[xy]+z",
        r"[|]+",  # a | in a class, or escaped, is not an alternative
        r"\(a|b\)+",
        r"(?:/[|a-z]x)+",
        r"(?:a\|b)+",
    ],
)
def test_common_path_patterns_are_still_accepted(pattern):
    assert (
        SitemapRecipe(sitemap_url=SITEMAP_URL, include_pattern=pattern).include_pattern == pattern
    )
