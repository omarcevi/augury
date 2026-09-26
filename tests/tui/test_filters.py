import pytest

from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.tui.layout import layout_for
from augury.tui.query import ItemFilter
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import ChoiceModal, PickerModal
from tests.tui.conftest import NOW


def seed(app):
    store_items(
        app.conn,
        [
            RawItem(
                source_id="hf-papers",
                kind="paper",
                arxiv_id="2609.00001",
                title="Qwen3-30B-A3B beats GRPO",
                url="https://huggingface.co/papers/2609.00001",
            ),
            RawItem(source_id="hf-blog", url="https://x/a", title="Fast decoding"),
            RawItem(source_id="hf-community", url="https://x/b", title="Community notes"),
        ],
        now=NOW,
    )


async def test_typing_filters_the_table_live(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", "q", "w", "e", "n")
        await pilot.pause()
        assert app.query_one(ItemsTable).row_count == 1
        assert app.mode == "SEARCH"


async def test_escape_returns_to_the_table(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", "escape")
        assert app.focused is app.query_one(ItemsTable) and app.mode == "NORMAL"


async def test_sort_and_show_cycle_and_update_their_chips(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        assert app.query_one("#chip-sort", Chip).value == "Score ↓"  # the Digest preset's
        await pilot.press("s")
        assert (
            app.item_filter.sort == "newest"
            and app.query_one("#chip-sort", Chip).value == "Newest ↓"
        )
        await pilot.press("v")
        assert app.item_filter.show == "new" and app.query_one("#chip-show", Chip).value == "New"
        await pilot.press("v")
        assert app.item_filter.show == "all" and app.query_one("#chip-show", Chip).value == "All"


async def test_source_picker_filters_the_table(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("S")
        assert isinstance(app.screen, PickerModal)
        await pilot.press("space", "enter")  # tick the first source (hf-blog) and apply
        await pilot.pause()
        assert app.item_filter.sources == frozenset({"hf-blog"})
        assert app.query_one(ItemsTable).row_count == 1
        assert app.query_one("#chip-sources", Chip).value == "hf-blog"


async def test_escape_in_a_picker_changes_nothing(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("K", "space", "escape")
        await pilot.pause()
        assert app.item_filter.kinds == frozenset()


async def test_date_choice(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("D")
        assert isinstance(app.screen, ChoiceModal)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.item_filter.date == "7d" and app.query_one("#chip-date", Chip).value == "7 days"


async def test_every_chip_shows_its_full_title_at_80_and_120_columns(make_app):
    # "Sources (S)" is 11 chars; the old fixed `min-width: 14` in theme.tcss cut it to
    # "Sources…" (user screenshot, 2026-09-26). Each chip's min-width must cover its own
    # border title, whatever the terminal width.
    for width in (80, 120):
        app = make_app()
        async with app.run_test(size=(width, 40)) as pilot:
            await pilot.pause()
            chips = [chip for chip in app.query(Chip) if chip.display]  # no Theme below 160
            assert chips, "no chips mounted"
            svg = app.export_screenshot()
            for chip in chips:
                title = chip.border_title or ""
                # `.size` is the content box only (border-box minus border+padding); the
                # border title is drawn on the border row itself ("╭─ Title ─╮": 2 corners
                # + 1 dash + 1 space flanking each side = 6 cells of decoration), so the
                # *outer* size is what must cover it. (The chips row is wider than either
                # terminal size even before this fix -- #filters scrolls horizontally --
                # so a chip past the right edge is merely off-screen, not truncated; this
                # geometry check is what actually proves "full title, never ellipsized".)
                assert chip.outer_size.width >= len(title) + 6, (
                    width,
                    title,
                    chip.outer_size.width,
                )
                if chip.region.right <= width:  # only assert on-screen chips render intact
                    assert title.replace(" ", "&#160;") in svg, (width, title)


def _chip_value_line(chip: Chip) -> str:
    """The chip's own rendered content line (row 0 of its `render_line` coordinate
    space, which -- unlike `.region` -- is already relative to the padded content box,
    excluding the border Textual draws around it)."""
    return chip.render_line(0).text


async def test_a_chip_value_too_long_for_max_width_ends_with_an_ellipsis(make_app):
    # Chip's own width is `auto` with no upper bound, so today it just keeps growing to
    # fit *any* value (verified directly: a 62-char value grows the box to match, with no
    # wrapping and no truncation at all) -- which is exactly how "Unread" et al. never
    # needed an ellipsis before, but also means an untrusted, arbitrarily long source name
    # could stretch the whole filter row indefinitely. `.chip` needs its own ceiling
    # (`max-width`) for `text-overflow: ellipsis` to ever have anything to do -- Textual
    # 8.2.8's render() Text loses no_wrap/overflow (the project-wide quirk), so both the
    # cap and the ellipsis must come from CSS, not from `safe_text.text(..., one_line=True)`.
    app = make_app()
    async with app.run_test(size=(300, 40)) as pilot:
        chip = app.query_one("#chip-sources", Chip)
        chip.set_value("extremely-long-source-name-that-is-way-too-long-for-this-box")
        await pilot.pause()
        assert chip.outer_size.width <= 24  # bounded, not growing to fit the whole value
        assert _chip_value_line(chip).rstrip().endswith("…")


async def test_every_visible_chip_value_is_intact_or_ellipsized_at_90_columns(make_app):
    # P6 knock-on: widening the Sources chip pushed the row further right, so at 90
    # columns the Show chip's box straddles the screen edge and its value ("Unread") was
    # showing as the raw fragment "Unrea" with no ellipsis (the regenerated
    # test_reader_snapshots_size0_ SVG). That specific case is the viewport clipping a
    # chip that's only half on-screen -- a hard crop of an otherwise fully rendered
    # widget, at a different layer than any widget's own text-overflow CSS (the #filters
    # row is wider than 90 columns even with every value at its shortest, exactly as it
    # already is at 80 and 120 columns above; it scrolls horizontally by design). No CSS
    # can put an ellipsis at the exact cell the viewport happens to crop. What CSS *can*
    # guarantee, and what this checks, is that any chip fully inside the viewport never
    # shows a bare mid-word cut without a trailing ellipsis.
    app = make_app()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        for chip in app.query(Chip):
            if not chip.display or chip.region.right > 90:  # no Theme chip below 160 columns
                continue  # off-screen (partially or fully): a viewport crop, not text CSS
            shown = _chip_value_line(chip).rstrip()
            assert shown == chip.value or shown.endswith("…"), (chip.id, shown, chip.value)


# The widest value each chip can show (a long source name, "2 selected", "Shortest ↑", ...).
LONGEST = ItemFilter(
    sources=frozenset({"a-very-long-source-name-from-a-feed"}),
    kinds=frozenset({"paper", "article"}),
    tags=frozenset({"a-very-long-tag-name-from-triage"}),
    date="30d",
    sort="reading_time",
    show="hidden",
)


@pytest.mark.parametrize("longest", [False, True], ids=["defaults", "longest"])
@pytest.mark.parametrize("width", [80, 90, 99, 100, 120, 159, 160])
async def test_the_filter_row_fits_the_screen(make_app, width, longest):
    # At 90 columns the Show chip straddled the screen edge ("Unrea") and Theme was off it.
    app = make_app()
    async with app.run_test(size=(width, 30)) as pilot:
        if longest:
            app.theme = "catppuccin-mocha"  # the longest theme name `t` cycles through
            app.apply_filter(LONGEST)
        await pilot.pause()
        screen = app.screen._compositor.render_strips()  # what the terminal shows
        chips = [chip for chip in app.query(Chip) if chip.display]
        search = app.query_one("#search")
        assert search.region.right <= chips[0].region.x and search.region.width >= 7
        for chip in chips:
            region = chip.region
            assert region.x >= 0 and region.right <= width, (chip.id, region)
            top = screen[region.y].text[region.x : region.right]
            assert f" {chip.border_title} " in top, (chip.id, top)
            shown = screen[region.y + 1].text[region.x : region.right].strip().strip("│").strip()
            if shown != chip.value:  # cut, then only ever with an ellipsis
                assert shown.endswith("…") and chip.value.startswith(shown[:-1]), (chip.id, shown)
        # `t` still cycles the theme (and says so in the hints); only a wide screen has room.
        # Below 100 columns the Tags chip goes too (`#` still opens the picker).
        assert len(chips) == {"wide": 7, "medium": 6, "narrow": 5}[layout_for(width)]


def rows_of(app, widget) -> list[str]:
    """The widget's rows as the terminal shows them (borders and titles included)."""
    screen = app.screen._compositor.render_strips()
    region = widget.region
    return [screen[y].text[region.x : region.right] for y in range(region.y, region.bottom)]


@pytest.mark.parametrize("leave", ["enter", "escape"])
async def test_the_search_box_takes_the_whole_row_while_typing_at_80_columns(make_app, leave):
    # At 80 columns it's 7 cells wide: the title showed as "…" and a query as "dif".
    app = make_app()
    seed(app)
    query = "diffusiontransformer"  # 20 characters
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("slash", *query)
        await pilot.pause()
        search = app.query_one("#search")
        assert search.region.width == 80
        top, middle, _bottom = rows_of(app, search)
        assert "Search (/)" in top and query in middle
        whole_row = "".join(rows_of(app, app.query_one("#filters")))
        assert "Sources (S)" not in whole_row and "╭" not in whole_row  # no chip, not even part
        await pilot.press(leave)
        await pilot.pause()
        assert search.region.width == 7 and app.item_filter.search == query
        chips = [chip for chip in app.query(Chip) if chip.display]
        assert len(chips) == 5 and all(
            0 < chip.region.x < chip.region.right <= 80 for chip in chips
        )


HOSTILE = "[b]x[/b]"


async def test_a_narrow_items_pane_shows_the_search_in_its_title(make_app):
    # The search box can only show a few characters of it at 80 columns.
    app = make_app()
    seed(app)
    store_items(app.conn, [RawItem(source_id="hf-blog", url="https://x/h", title=HOSTILE)], now=NOW)
    keys = ["left_square_bracket", "b", "right_square_bracket", "x"]
    keys += ["left_square_bracket", "slash", "b", "right_square_bracket"]
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("slash", *keys, "enter")
        await pilot.pause()
        pane = app.query_one("#items-pane")
        assert app.item_filter.search == HOSTILE  # literally, and never parsed as markup
        assert rows_of(app, pane)[0].startswith('╭─ Items (1/4) · "[b]x[/b]" ─')
        await pilot.resize_terminal(120, 24)  # the search box shows it itself
        await pilot.pause()
        assert rows_of(app, pane)[0].startswith("╭─ Items (1/4) ─")
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert '· "[b]x[/b]"' in rows_of(app, pane)[0]
        await pilot.press("slash", *["backspace"] * len(HOSTILE), "enter")
        await pilot.pause()
        assert rows_of(app, pane)[0].startswith("╭─ Items (4/4) ─")


@pytest.mark.parametrize(("width", "toast"), [(100, True), (159, True), (160, False)])
async def test_t_names_the_theme_when_its_chip_is_hidden(make_app, width, toast):
    app = make_app()
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        notes = [(n.message, n.markup) for n in app._notifications if n.message.startswith("Theme")]
        assert notes == ([(f"Theme: {app.theme}", False)] if toast else [])
