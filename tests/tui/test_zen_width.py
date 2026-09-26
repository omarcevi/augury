"""P10: zen mode caps the text column at [tui] reading_width and centres it."""

import pytest

from augury.core.config import Config, ScoutConfig, TuiConfig
from augury.core.db.state_repo import StateRepo
from augury.tui.widgets.reader_pane import ReaderPane
from tests.tui.conftest import until
from tests.tui.test_reader import LONG_MD, cache, open_first, progress, seed

CODE_LINE = "x = [" + ", ".join(str(i) for i in range(80)) + "]"
MD = LONG_MD + f"\n\n```python\n{CODE_LINE}\n```\n\n" + "word " * 200


def text_lines(app) -> list[str]:
    lines: list[str] = []
    for paragraph in app.query("MarkdownParagraph"):
        for y in range(paragraph.size.height):
            lines.append(paragraph.render_line(y).text.rstrip())
    return [line for line in lines if line.strip()]


def margins(app) -> tuple[int, int]:
    viewer = app.query_one(ReaderPane).viewer
    column, view = viewer.document.region, viewer.scrollable_content_region
    return column.x - view.x, view.right - column.right


async def zen_at(pilot, width: int) -> None:
    await pilot.resize_terminal(width, 50)
    await open_first(pilot)
    await pilot.press("z")
    await until(pilot, lambda: pilot.app.reader_settled)
    await pilot.pause()


@pytest.mark.parametrize("width", [88, 60])
async def test_zen_caps_and_centres_the_text_column(make_app, width):
    config = Config(scout=ScoutConfig(auto_after_hours=0), tui=TuiConfig(reading_width=width))
    app = make_app(config=config)
    cache(app, seed(app)[0], MD)
    async with app.run_test(size=(200, 50)) as pilot:
        await zen_at(pilot, 200)
        lines = text_lines(app)
        assert max(len(line) for line in lines) <= width
        assert max(len(line) for line in lines) > width - 10  # it does use the column
        left, right = margins(app)
        assert left > 20 and abs(left - right) <= 1


async def test_code_blocks_still_scroll_sideways_in_zen(make_app):
    app = make_app()
    cache(app, seed(app)[0], MD)
    async with app.run_test(size=(200, 50)) as pilot:
        await zen_at(pilot, 200)
        fence = app.query_one("MarkdownFence")
        assert fence.region.width <= 88 and fence.max_scroll_x > 0


async def test_a_narrower_zen_uses_the_whole_width(make_app):
    app = make_app()
    cache(app, seed(app)[0], MD)
    async with app.run_test(size=(80, 50)) as pilot:
        await zen_at(pilot, 80)
        left, _ = margins(app)
        assert left == 0


async def test_zen_on_and_off_keeps_the_reading_position(make_app):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id, MD)
    StateRepo(app.conn).set_progress(item_id, 0.5, now=app.now())
    async with app.run_test(size=(200, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        for _ in range(2):
            before = viewer.virtual_size
            await pilot.press("z")
            await until(pilot, lambda b=before: viewer.virtual_size != b and app.reader_settled)
            assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)
        await pilot.press("j")
        await pilot.pause()
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.05)


# A 9-column results table, as in a paper: far wider than the 88-cell column.
HEADERS = [f"Column header {i}" for i in range(9)]
ROWS = [[f"0.{r}{i}23456±0.0{i}" for i in range(9)] for r in range(4)]
WIDE_TABLE = "\n".join("| " + " | ".join(cells) + " |" for cells in [HEADERS, ["---"] * 9, *ROWS])
TABLE_MD = LONG_MD + f"\n\n## Results\n\n{WIDE_TABLE}\n\n" + "word " * 400


def cell_texts(table) -> list[str]:
    return [cell.render_line(0).text.strip() for cell in table.query("MarkdownTableCellContents")]


@pytest.mark.parametrize(("size", "zen"), [((200, 50), True), ((180, 50), False)])
async def test_wide_tables_keep_whole_cells_and_scroll_sideways(make_app, size, zen):
    app = make_app()
    cache(app, seed(app)[0], TABLE_MD)
    async with app.run_test(size=size) as pilot:
        await open_first(pilot)
        if zen:
            await pilot.press("z")
            await until(pilot, lambda: app.reader_settled)
        await pilot.pause()
        table = app.query_one("MarkdownTable")
        assert table.region.width <= 88 + 4
        assert table.max_scroll_x > 0  # it scrolls instead of squeezing the columns
        texts = cell_texts(table)
        assert set(HEADERS) | {cell for row in ROWS for cell in row} == set(texts)
        assert not any("…" in text for text in texts)


async def test_zen_keeps_the_position_with_a_wide_table(make_app):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id, TABLE_MD)
    async with app.run_test(size=(200, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
        await until(pilot, lambda: progress(app, item_id) > 0)
        for _ in range(2):
            before = viewer.virtual_size
            await pilot.press("z")
            await until(pilot, lambda b=before: viewer.virtual_size != b and app.reader_settled)
            assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)
        await pilot.press("j")
        await pilot.pause()
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.05)


# Tables of notes, common in HF blog posts: their cells should wrap, not scroll.
NOTE = (
    "This model is a strong general-purpose baseline that handles long documents, code, and "
    "multilingual chat with a 128k context window and permissive licence."
)
PROSE_TABLE = "| Model | Notes |\n|---|---|\n" + "\n".join(
    f"| model-{i} | {NOTE} |" for i in range(4)
)
PROSE_MD = LONG_MD + f"\n\n## Models\n\n{PROSE_TABLE}\n\n" + "word " * 400


def cell_text(cell) -> str:
    return " ".join(cell.render_line(y).text.strip() for y in range(cell.size.height)).strip()


@pytest.mark.parametrize(("size", "zen"), [((180, 50), False), ((200, 50), True)])
async def test_tables_of_prose_wrap_their_cells_instead_of_scrolling(make_app, size, zen):
    app = make_app()
    cache(app, seed(app)[0], PROSE_MD)
    async with app.run_test(size=size) as pilot:
        await open_first(pilot)
        if zen:
            await pilot.press("z")
            await until(pilot, lambda: app.reader_settled)
        await pilot.pause()
        table = app.query_one("MarkdownTable")
        assert table.max_scroll_x == 0  # it fits the column: no sideways scrolling
        notes = [c for c in table.query("MarkdownTableCellContents") if "baseline" in cell_text(c)]
        assert len(notes) == 4
        for cell in notes:
            assert 1 < cell.size.height <= 6  # wrapped onto a few lines ...
            assert cell_text(cell) == NOTE  # ... and nothing cut


async def test_zen_keeps_the_position_with_a_table_of_prose(make_app):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id, PROSE_MD)
    async with app.run_test(size=(200, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
        await until(pilot, lambda: progress(app, item_id) > 0)
        for _ in range(2):
            before = viewer.virtual_size
            await pilot.press("z")
            await until(pilot, lambda b=before: viewer.virtual_size != b and app.reader_settled)
            assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)
        await pilot.press("j")
        await pilot.pause()
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.05)
