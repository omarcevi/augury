from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from augury.core.models import RECIPE_ADAPTER, FetchState, HtmlListingRecipe, Source
from augury.sources.base import AdapterError
from augury.sources.html_listing import HtmlListingAdapter, parse_listing
from augury.sources.http import PoliteClient
from augury.sources.registry import ADAPTERS
from tests.helpers import FakeTime, allow_robots

PAGES = Path(__file__).parent.parent / "fixtures" / "pages"
ORIGIN = "https://labs.example.com"
LISTING_URL = f"{ORIGIN}/research/"
LISTING = (PAGES / "listing_research.html").read_text()
RECIPE = HtmlListingRecipe(
    listing_url=LISTING_URL,
    item_selector="article.post-card",
    link_selector="h2 a",
    title_selector="h2",
    date_selector="time, .date",
)
SOURCE = Source(id="labs", name="Labs", origin="user", recipe=RECIPE)


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


def test_the_recipe_round_trips():
    assert RECIPE_ADAPTER.validate_json(RECIPE.model_dump_json()) == RECIPE


@pytest.mark.parametrize("bad", ["", "   ", "div[", "a:::b"])
def test_an_invalid_selector_is_refused(bad):
    with pytest.raises(ValidationError, match="selector"):
        RECIPE.model_validate({**RECIPE.model_dump(), "item_selector": bad})


def test_items_resolve_relative_links_and_skip_unusable_cards():
    items, skipped = parse_listing(LISTING, LISTING_URL, RECIPE, "labs")
    assert [i.url for i in items] == [
        f"{ORIGIN}/research/2026/world-models",
        f"{ORIGIN}/research/2026/sparse-attention",
        f"{ORIGIN}/research/research/2026/agents-eval",
    ]
    assert items[1].title == "Sparse attention, & why it works"
    assert skipped == 2  # no link; a javascript: link


def test_dates_come_from_datetime_attributes_iso_text_or_the_format():
    items, _ = parse_listing(LISTING, LISTING_URL, RECIPE, "labs")
    assert items[0].published_at is not None and items[0].published_at.day == 24
    assert items[1].published_at is not None and items[1].published_at.day == 19
    assert items[2].published_at is None  # "September 12, 2026" needs a date_format
    with_format = RECIPE.model_copy(update={"date_format": "%B %d, %Y"})
    items, _ = parse_listing(LISTING, LISTING_URL, with_format, "labs")
    assert items[2].published_at is not None and items[2].published_at.day == 12


def test_an_item_selector_matching_nothing_is_a_failure():
    recipe = RECIPE.model_copy(update={"item_selector": "div.gone"})
    with pytest.raises(AdapterError, match="matched 0 elements"):
        parse_listing(LISTING, LISTING_URL, recipe, "labs")


def test_inner_selectors_matching_nothing_are_a_failure():
    recipe = RECIPE.model_copy(update={"title_selector": "h5.nope"})
    with pytest.raises(AdapterError, match="matched nothing inside"):
        parse_listing(LISTING, LISTING_URL, recipe, "labs")


def test_hostile_titles_become_plain_text():
    page = (
        '<article class="post-card"><h2><a href="/x">'
        "[bold red]Hi\x1b[31m <b>there</b></a></h2></article>"
    )
    items, _ = parse_listing(page, LISTING_URL, RECIPE, "labs")
    assert items[0].title == "[bold red]Hi there"  # markup stays literal; the TUI escapes it


def test_a_huge_listing_is_cut_to_the_first_50_matches():
    page = "".join(
        f'<article class="post-card"><h2><a href="/p{i}">P{i}</a></h2></article>'
        for i in range(500)
    )
    items, _ = parse_listing(page, LISTING_URL, RECIPE, "labs")
    assert len(items) == 50


async def test_fetch_reads_the_page_and_remembers_its_etag(http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(LISTING_URL).mock(
        return_value=httpx.Response(200, text=LISTING, headers={"ETag": '"l1"'})
    )
    result = await HtmlListingAdapter().fetch(SOURCE, FetchState(), http)
    assert len(result.items) == 3 and result.skipped == 2 and result.state.etag == '"l1"'
    route.mock(return_value=httpx.Response(304))
    again = await HtmlListingAdapter().fetch(SOURCE, result.state, http)
    assert again.not_modified and again.items == []


async def test_a_page_that_changed_shape_is_a_failure(http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(LISTING_URL).mock(
        return_value=httpx.Response(200, text="<html><body><p>Redesigned!</p></body></html>")
    )
    with pytest.raises(AdapterError, match="matched 0 elements"):
        await HtmlListingAdapter().fetch(SOURCE, FetchState(), http)


def test_the_registry_knows_html_listings():
    assert isinstance(ADAPTERS["html_listing"], HtmlListingAdapter)
