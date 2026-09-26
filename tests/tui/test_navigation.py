"""P4: vim-style navigation in the items table and the reader."""

from datetime import timedelta

import pytest

from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.tui.keymap import KEYMAP
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import PickerModal
from augury.tui.widgets.reader_pane import ReaderPane
from tests.helpers import CountingHttp
from tests.tui.conftest import NOW
from tests.tui.test_reader import cache, open_first


def seed_many(app, n: int = 40) -> list[str]:
    raws = [
        RawItem(
            source_id="hf-blog",
            url=f"https://x/{i}",
            title=f"Post {i}",
            summary=f"Summary of post {i}.",
            published_at=NOW - timedelta(hours=i + 1),
        )
        for i in range(n)
    ]
    return store_items(app.conn, raws, now=NOW).new_ids


async def test_g_and_shift_g_jump_to_the_first_and_last_row(make_app):
    app = make_app()
    ids = seed_many(app)
    async with app.run_test(size=(180, 30)) as pilot:
        table = app.query_one(ItemsTable)
        await pilot.press("G")
        assert table.current_row().id == ids[-1]  # type: ignore[union-attr]
        await pilot.press("g")
        assert table.current_row().id == ids[0]  # type: ignore[union-attr]


async def test_ctrl_d_and_ctrl_u_move_half_a_page_in_the_table(make_app):
    app = make_app()
    seed_many(app)
    async with app.run_test(size=(180, 30)) as pilot:
        table = app.query_one(ItemsTable)
        half = (table.scrollable_content_region.height - table.header_height) // 2
        assert half > 1
        await pilot.press("ctrl+d")
        assert table.cursor_row == half
        await pilot.press("ctrl+d")
        assert table.cursor_row == 2 * half
        await pilot.press("ctrl+u")
        assert table.cursor_row == half
        await pilot.press("ctrl+u", "ctrl+u")  # stops at the first row
        assert table.cursor_row == 0


async def test_page_keys_still_work_in_the_table(make_app):
    app = make_app()
    seed_many(app)
    async with app.run_test(size=(180, 30)) as pilot:
        table = app.query_one(ItemsTable)
        await pilot.press("pagedown")
        assert table.cursor_row > 5
        await pilot.press("pageup")
        assert table.cursor_row == 0


async def test_g_and_shift_g_go_to_the_top_and_bottom_of_the_article(make_app):
    app = make_app()
    cache(app, seed_many(app, 3)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        await pilot.press("G")
        await pilot.pause()
        assert viewer.max_scroll_y > 0 and viewer.scroll_y == viewer.max_scroll_y
        await pilot.press("g")
        await pilot.pause()
        assert viewer.scroll_y == 0


async def test_ctrl_d_and_ctrl_u_scroll_the_article_half_a_page(make_app):
    app = make_app()
    cache(app, seed_many(app, 3)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        half = viewer.scrollable_content_region.height // 2
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert viewer.scroll_y == half
        await pilot.press("ctrl+d", "ctrl+u")
        await pilot.pause()
        assert viewer.scroll_y == half


async def test_shift_j_and_k_scroll_five_lines_and_shadow_the_kind_picker(make_app):
    app = make_app()
    cache(app, seed_many(app, 3)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        await pilot.press("J", "J")
        await pilot.pause()
        assert viewer.scroll_y == 10
        await pilot.press("K")
        await pilot.pause()
        assert viewer.scroll_y == 5
        assert not isinstance(app.screen, PickerModal)  # K scrolled; no Kind picker
        await pilot.press("j", "k", "space")  # the old keys still work
        await pilot.pause()
        assert viewer.scroll_y > 10


async def test_shift_k_still_opens_the_kind_picker_from_the_table(make_app):
    app = make_app()
    seed_many(app, 3)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("K")
        await pilot.pause()
        assert isinstance(app.screen, PickerModal)


@pytest.mark.parametrize(
    ("mode", "hints"),
    [
        ("NORMAL", {"g/G", "^d/^u"}),
        ("READ", {"g/G", "^d/^u", "J/K", "y", "Y"}),
    ],
)
def test_new_keys_are_in_the_hints(mode, hints):
    keys = {hint.key for hint in KEYMAP[mode]}
    assert hints <= keys


async def test_help_lists_the_navigation_keys(make_app):
    app = make_app()
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpOverlay)
        body = str(app.screen.query_one("#help-text").render())
        for key in ("g/G", "^d/^u", "J/K"):
            assert key in body


async def test_focus_moving_in_and_out_of_the_article_does_not_restyle_all_of_it(
    make_app, monkeypatch
):
    # P4 profiling: Textual restyles a widget *and every descendant* when its focus changes.
    # With a 28k-word paper that's ~3.6k widgets and about a second for esc, a click on the
    # list, or opening the contents -- and nothing in the article is styled by its focus.
    app = make_app()
    cache(app, seed_many(app, 3)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        restyled: list[str] = []
        update_nodes = app.stylesheet.update_nodes

        def spy(nodes, animate=False):
            nodes = list(nodes)
            restyled.extend(type(node).__name__ for node in nodes)
            update_nodes(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", spy)
        viewer = app.query_one(ReaderPane).viewer
        app.query_one(ItemsTable).focus()
        await pilot.pause()
        viewer.document.focus()
        await pilot.pause()
        assert app.focused is viewer.document
        assert restyled  # the focus changes did restyle something ...
        assert "MarkdownParagraph" not in restyled  # ... but not the article's blocks


def test_a_focus_change_without_an_active_app_is_harmless():
    # DOMNode.update_node_styles guards against this; the override keeps the guard.
    from augury.tui.widgets.reader_pane import ReaderDocument

    ReaderDocument().watch_has_focus(True)


NAV_KEYS = ("g", "G", "ctrl+d", "ctrl+u", "up", "down", "pageup", "pagedown", "home", "end")


@pytest.mark.parametrize("how", ["first launch", "search with no hits"])
async def test_navigation_keys_on_an_empty_table_are_harmless(make_app, how):
    # G on an empty table used to crash in the app's RowHighlighted handler (row_key None);
    # first launch, "Show: Saved" with nothing saved and a search with no hits all get there.
    app = make_app()
    if how == "search with no hits":
        seed_many(app, 3)
    async with app.run_test(size=(180, 40)) as pilot:
        if how == "search with no hits":
            await pilot.press("slash", *"zzzz", "enter")
            await pilot.pause()
        table = app.query_one(ItemsTable)
        assert table.row_count == 0 and app.focused is table
        for key in (*NAV_KEYS, "enter"):
            await pilot.press(key)
            await pilot.pause()
        assert app.reading_id is None and app.mode == "NORMAL"


async def test_reader_keys_without_an_article_are_harmless(make_app):
    # Extraction failed: the reader shows only the summary, with no headings or code.
    app = make_app(http=CountingHttp())
    seed_many(app, 3)
    async with app.run_test(size=(180, 40)) as pilot:
        await open_first(pilot)
        for key in ("g", "G", "ctrl+d", "ctrl+u", "J", "K", "y", "Y", "[", "]", "c", "c"):
            await pilot.press(key)
            await pilot.pause()
        assert app.mode == "READ"
