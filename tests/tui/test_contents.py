"""P7: the reader's contents sidebar wraps long headings onto two lines."""

import pytest
from textual.widgets import OptionList

from augury.tui.widgets.contents_list import ContentsList
from augury.tui.widgets.reader_pane import ReaderPane
from tests.tui.conftest import until
from tests.tui.test_reader import LONG_MD, cache, open_first, progress, seed

# The user's example (2026-09-26), cut to "Add memory to the agent you alr…" by the old Tree.
LONG_HEADING = "Add memory to the agent you already built"
HUGE_HEADING = " ".join(["An extremely long section heading from a real paper"] * 4)


def at_top(viewer, heading) -> bool:
    """The heading is at the top of the viewport (scroll_to_widget keeps its top margin)."""
    return viewer.scroll_y == heading.virtual_region_with_margin.y


def entry_lines(contents: ContentsList, index: int) -> list[str]:
    """The rendered lines of one contents entry, as plain text."""
    options = contents.query_one(OptionList)
    lines = options._lines  # (option index, line within the option) per rendered line
    return [
        options.render_line(y - int(options.scroll_y)).text.rstrip()
        for y, (i, _) in enumerate(lines)
        if i == index
    ]


async def open_contents(pilot, md: str):
    app = pilot.app
    await open_first(pilot)
    await pilot.press("c")
    contents = app.query_one(ContentsList)
    options = contents.query_one(OptionList)
    await until(pilot, lambda: options.option_count > 0 and options.size.width > 0)
    await until(pilot, lambda: app.reader_settled)
    return contents, options


async def test_the_reader_uses_the_wrapping_contents_list(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)):
        viewer = app.query_one(ReaderPane).viewer
        assert isinstance(viewer.table_of_contents, ContentsList)
        assert not app.query("Tree")  # Textual's one-line-per-heading tree is gone


async def test_a_long_heading_wraps_onto_two_lines(make_app):
    app = make_app()
    md = f"# Title\n\n## Short\n\nwords\n\n## {LONG_HEADING}\n\n" + "word " * 400
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(180, 50)) as pilot:
        contents, options = await open_contents(pilot, md)
        # The 40 % cap keeps it narrower than the heading, so it has to wrap.
        assert options.size.width < len(LONG_HEADING)
        lines = entry_lines(contents, 2)
        assert len(lines) == 2
        assert " ".join(line.strip() for line in lines) == LONG_HEADING
        assert "…" not in "".join(lines)


async def test_a_heading_longer_than_two_lines_ends_in_an_ellipsis(make_app):
    app = make_app()
    md = f"# Title\n\n## {HUGE_HEADING}\n\n" + "word " * 400
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(120, 50)) as pilot:
        contents, _ = await open_contents(pilot, md)
        lines = entry_lines(contents, 1)
        assert len(lines) == 2
        assert lines[1].endswith("…")
        assert HUGE_HEADING.startswith(lines[0].strip())


async def test_entries_are_indented_by_heading_level(make_app):
    app = make_app()
    md = "# Title\n\n## Section\n\n### Subsection\n\n" + "word " * 400
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(180, 50)) as pilot:
        contents, _ = await open_contents(pilot, md)
        title, section, subsection = (entry_lines(contents, i)[0] for i in range(3))
        indent = [len(line) - len(line.lstrip()) for line in (title, section, subsection)]
        assert indent[0] < indent[1] < indent[2]
        assert (title.strip(), section.strip(), subsection.strip()) == (
            "Title",
            "Section",
            "Subsection",
        )


async def test_heading_text_is_never_markup(make_app):
    app = make_app()
    md = "# Title\n\n## [bold red]Evil[/] \\[link=https://x]y\n\n" + "word " * 50
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(180, 50)) as pilot:
        contents, _ = await open_contents(pilot, md)
        assert "[bold red]Evil[/]" in entry_lines(contents, 1)[0]


async def test_selecting_an_entry_jumps_to_its_heading(make_app):
    app = make_app()
    item_id = seed(app)[0]
    md = f"# Title\n\n## {LONG_HEADING}\n\n" + "word " * 600 + "\n\n## The end\n\n" + "word " * 900
    cache(app, item_id, md)
    async with app.run_test(size=(180, 50)) as pilot:
        _, options = await open_contents(pilot, md)
        viewer = app.query_one(ReaderPane).viewer
        target = app.query("MarkdownH2").last()
        index = options.option_count - 1  # "The end", below the two-line entry
        await pilot.click(options, offset=(2, 1 + list(options._lines).index((index, 0))))
        await until(pilot, lambda: at_top(viewer, target))
        await until(pilot, lambda: progress(app, item_id) > 0)


async def test_keyboard_selection_jumps_too(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        _, options = await open_contents(pilot, LONG_MD)
        viewer = app.query_one(ReaderPane).viewer
        options.focus()
        await pilot.press("down", "down", "down", "enter")  # Title, Section 0, Section 1
        target = app.query("MarkdownH2")[1]
        await until(pilot, lambda: at_top(viewer, target))


@pytest.mark.parametrize("presses", [1, 2])
async def test_toggling_contents_keeps_the_progress(make_app, presses):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
        await until(pilot, lambda: progress(app, item_id) > 0)
        for _ in range(presses):
            before = viewer.virtual_size
            await pilot.press("c")
            await until(pilot, lambda b=before: viewer.virtual_size != b and app.reader_settled)
        await pilot.press("j")
        await pilot.pause()
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.05)
        assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)


async def test_hiding_the_contents_hands_focus_back_to_the_article(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        _, options = await open_contents(pilot, LONG_MD)
        options.focus()
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        viewer = app.query_one(ReaderPane).viewer
        assert app.focused is viewer.document
        await pilot.press("j")
        await pilot.pause()
        assert viewer.scroll_y == 1


async def test_brackets_still_jump_between_sections_with_contents_open(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_contents(pilot, LONG_MD)
        viewer = app.query_one(ReaderPane).viewer
        await pilot.press("right_square_bracket", "right_square_bracket")
        await pilot.pause()
        assert viewer.scroll_y == app.query("MarkdownH2").first().virtual_region.y


async def test_toggling_contents_does_not_restyle_the_article(make_app, monkeypatch):
    # MarkdownViewer shows its contents through a CSS class on itself, and a class change
    # restyles every descendant: ~1 s per `c` on a 28k-word paper (P4 profiling).
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        restyled: list[str] = []
        update_nodes = app.stylesheet.update_nodes

        def spy(nodes, animate=False):
            nodes = list(nodes)
            restyled.extend(type(node).__name__ for node in nodes)
            update_nodes(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", spy)
        contents = app.query_one(ContentsList)
        await pilot.press("c")
        await until(pilot, lambda: contents.display and contents.size.width > 0)
        await pilot.press("c")
        await until(pilot, lambda: not contents.display)
        await pilot.pause()
        assert "MarkdownParagraph" not in restyled


def test_contents_snapshot(make_app, snap_compare):
    app = make_app()
    md = (
        f"# Title\n\n## Short\n\nwords\n\n## {LONG_HEADING}\n\nwords\n\n"
        f"### A subsection\n\nwords\n\n## {HUGE_HEADING}\n\n" + "word " * 400
    )
    cache(app, seed(app)[0], md)

    async def run_before(pilot) -> None:
        await open_contents(pilot, md)

    assert snap_compare(app, terminal_size=(180, 40), run_before=run_before)


async def test_indentation_follows_the_levels_in_use_and_stays_shallow(make_app):
    # arXiv's LaTeXML makes "Abstract" an h6 and paragraph titles h5: with raw levels those
    # were indented 8-10 cells deep in a sidebar that is ~30 cells wide.
    app = make_app()
    md = "# Title\n\n###### Abstract\n\n## 1 Intro\n\n### 1.1 Sub\n\n##### Para.\n\n" + "w " * 300
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(180, 50)) as pilot:
        contents, _ = await open_contents(pilot, md)
        lines = [entry_lines(contents, i)[0] for i in range(5)]
        indent = {line.strip(): len(line) - len(line.lstrip()) for line in lines}
        assert indent == {"Title": 0, "Abstract": 6, "1 Intro": 2, "1.1 Sub": 4, "Para.": 6}


async def test_an_article_without_headings_says_so(make_app):
    app = make_app()
    md = "Just a paragraph, no headings at all.\n\n" + "word " * 300
    cache(app, seed(app)[0], md)
    async with app.run_test(size=(180, 50)) as pilot:
        contents, options = await open_contents(pilot, md)
        assert options.option_count == 1
        assert entry_lines(contents, 0) == ["No sections"]
        viewer = app.query_one(ReaderPane).viewer
        options.focus()
        await pilot.press("enter")  # nothing to jump to
        await pilot.pause()
        assert viewer.scroll_y == 0
