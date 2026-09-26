"""P11: "new since your last visit" -- the ✦ marker, Show: New and the health counter."""

from datetime import timedelta

import pytest
from textual.color import Color

from augury.agents.normalize import store_items
from augury.core.config import Config, ScoutConfig
from augury.core.models import RawItem
from augury.tui.health import HealthSnapshot, health_line
from augury.tui.keymap import THEMES
from augury.tui.query import ItemFilter
from augury.tui.ui_state import UiState, save
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.items_table import ItemsTable
from tests.helpers import HfHttp
from tests.tui.conftest import NOW, until

LAST_VISIT = NOW - timedelta(days=1)
EVERYTHING = ItemFilter(date="all")


def seed(app) -> tuple[list[str], list[str]]:
    """Two items first seen before the last visit, two after it."""
    old = [
        RawItem(source_id="hf-blog", url=f"https://x/old{i}", title=f"Old {i}") for i in range(2)
    ]
    new = [
        RawItem(source_id="hf-blog", url=f"https://x/new{i}", title=f"New {i}") for i in range(2)
    ]
    return (
        store_items(app.conn, old, now=LAST_VISIT - timedelta(hours=1)).new_ids,
        store_items(app.conn, new, now=NOW - timedelta(hours=1)).new_ids,
    )


def visited_before(paths) -> None:
    save(paths, UiState(current_visit_started_at=LAST_VISIT))


def marked(app) -> list[str]:
    table = app.query_one(ItemsTable)
    return sorted(
        row.title
        for key, row in table.rows_by_key.items()
        if table.get_row(key)[1].plain.startswith("✦ ")
    )


def listed(app) -> list[str]:
    return sorted(row.title for row in app.query_one(ItemsTable).rows_by_key.values())


def health(app) -> str:
    return app.query_one(HealthBar).render().plain


async def test_only_rows_first_seen_since_the_last_visit_are_marked(make_app, paths):
    visited_before(paths)
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)):
        app.apply_filter(EVERYTHING)
        assert listed(app) == ["New 0", "New 1", "Old 0", "Old 1"]
        assert marked(app) == ["New 0", "New 1"]
        assert "2 new since last visit" in health(app)


async def test_the_marker_sits_right_before_the_title_and_keeps_it_one_line(make_app, paths):
    visited_before(paths)
    app = make_app()
    _old, new = seed(app)
    async with app.run_test(size=(140, 40)):
        title = app.query_one(ItemsTable).get_row(new[1])[1]
        assert title.plain == "✦ New 1" and title.no_wrap
        assert app.query_one(ItemsTable).get_row(new[1])[0].plain == "●"  # the dot is its own


async def test_the_marker_clears_once_the_item_is_opened(make_app, paths):
    visited_before(paths)
    app = make_app()
    _old, new = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        table = app.query_one(ItemsTable)
        assert table.current_row().id == new[1]  # type: ignore[union-attr]
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        assert not table.get_row(new[1])[1].plain.startswith("✦")
        assert table.get_row(new[0])[1].plain.startswith("✦")
        assert "1 new since last visit" in health(app)  # the counter follows at once
        await pilot.press("escape")
        assert marked(app) == ["New 0"]


async def test_opening_in_the_browser_or_hiding_also_updates_the_counter(make_app, paths):
    visited_before(paths)
    app = make_app()
    seed(app)
    app.open_url = lambda url, **kwargs: None  # type: ignore[method-assign]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("o")
        assert "1 new since last visit" in health(app) and marked(app) == ["New 0"]
        await pilot.press("down", "x")
        assert "0 new since last visit" in health(app) and marked(app) == []


async def test_show_new_lists_exactly_the_marked_rows(make_app, paths):
    visited_before(paths)
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        app.apply_filter(EVERYTHING)
        await pilot.press("v")
        assert app.item_filter.show == "new"
        assert app.query_one("#chip-show", Chip).value == "New"
        assert listed(app) == marked(app) == ["New 0", "New 1"]
        count = int(health(app).split(" new since last visit")[0].rsplit(" ", 1)[-1])
        assert count == len(marked(app))


async def test_a_first_launch_counts_everything_as_new(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)):
        app.apply_filter(EVERYTHING)
        assert marked(app) == listed(app) == ["New 0", "New 1", "Old 0", "Old 1"]
        line = health(app)
        assert "· 4 new" in line and "since last visit" not in line


async def test_items_the_auto_scout_brings_in_are_new(make_app, paths):
    visited_before(paths)
    app = make_app(
        config=Config(scout=ScoutConfig(auto_after_hours=12, enrich_max_per_run=0)), http=HfHttp()
    )
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await until(pilot, lambda: len(listed(app)) > 2)
        scouted = [t for t in listed(app) if not t.startswith(("Old", "New"))]
        assert scouted and set(scouted) <= set(marked(app))
        count = int(health(app).split(" new since last visit")[0].rsplit(" ", 1)[-1])
        app.apply_filter(ItemFilter(date="all", show="new"))
        assert count == len(marked(app)) == len(listed(app))


def luminance(color: Color) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b)


def contrast(a: Color, b: Color) -> float:
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("theme", THEMES)
async def test_the_marker_is_readable_in_every_theme(make_app, paths, theme):
    visited_before(paths)
    app = make_app()
    _old, new = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        app.theme = theme
        await pilot.pause()
        app.refresh_colors()
        marker = app.query_one(ItemsTable).get_row(new[0])[1]
        style = marker.spans[0].style
        palette = app.get_css_variables()
        assert marker.plain[marker.spans[0].start : marker.spans[0].end] == "✦ "
        assert "bold" in str(style) and palette["text-accent"] in str(style)
        # WCAG's 3:1 for graphics; plain $accent is only 1.4:1 on textual-light's surface.
        assert contrast(Color.parse(palette["text-accent"]), Color.parse(palette["surface"])) >= 3


def test_the_health_line_says_new_since_last_visit_or_just_new_on_a_first_launch():
    snapshot = HealthSnapshot(NOW, None, None, new_items=5, new_since=LAST_VISIT)
    assert "Scout: never · 5 new since last visit  │" in health_line(snapshot, {}).plain
    first = HealthSnapshot(NOW, None, None, new_items=5, new_since=None)
    assert "Scout: never · 5 new  │" in health_line(first, {}).plain


async def test_a_long_health_line_is_clipped_with_an_ellipsis_not_wrapped_away(make_app, paths):
    visited_before(paths)
    app = make_app()
    store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url=f"https://x/{i}", title=f"T{i}") for i in range(123)],
        now=NOW,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        bar = app.query_one(HealthBar)
        shown = bar.render_line(0).text
        assert "123 new since last visit" in shown and shown.rstrip().endswith("…")


async def test_show_new_agrees_with_the_counter_at_the_default_date_today(make_app, paths):
    save(paths, UiState(current_visit_started_at=NOW - timedelta(days=2)))
    app = make_app()
    hours_ago = {"Before": 72, "Yesterday": 24, "Today": 1}  # the last visit was 48 h ago
    for title, hours in hours_ago.items():
        raw = RawItem(source_id="hf-blog", url=f"https://x/{title}", title=title)
        store_items(app.conn, [raw], now=NOW - timedelta(hours=hours))
    async with app.run_test(size=(140, 40)) as pilot:
        assert app.item_filter.date == "today" and listed(app) == ["Today"]
        assert "2 new since last visit" in health(app)
        await pilot.press("v")
        assert app.item_filter == ItemFilter(show="new")  # Date is still Today...
        assert listed(app) == marked(app) == ["Today", "Yesterday"]  # ...but doesn't apply
        count = int(health(app).split(" new since last visit")[0].rsplit(" ", 1)[-1])
        assert count == len(listed(app)) == 2
        assert app.query_one("#chip-date", Chip).value == "Last visit"
        await pilot.press("v")  # Show: All uses the Date chip again
        assert app.query_one("#chip-date", Chip).value == "Today" and listed(app) == ["Today"]
