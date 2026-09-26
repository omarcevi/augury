"""P8 (y copies a code block, Y the article) and P9 (copy on select). The real clipboard is
never touched: App.copy_to_clipboard, shutil.which and subprocess.run are all patched."""

import subprocess
from typing import Any

import pytest
from textual.app import App

from augury.core.config import Config, ScoutConfig, TuiConfig
from augury.tui import clipboard
from augury.tui.widgets.reader_pane import ReaderPane
from tests.tui.conftest import until
from tests.tui.test_reader import cache, open_first, seed

WIDE_LINE = "result = compute(" + ", ".join(f"argument_{i}" for i in range(30)) + ")"
CODE_1 = f"import os\n{WIDE_LINE}\nprint(result)"
CODE_2 = "def second():\n    return 2"
CODE_MD = (
    "# Title\n\nIntro paragraph.\n\n"
    f"```python\n{CODE_1}\n```\n\n"
    "Between the blocks.\n\n"
    f"```\n{CODE_2}\n```\n\n" + "\n\n".join("word " * 60 for _ in range(30))
)


class Clipboard:
    def __init__(self) -> None:
        self.osc52: list[str] = []
        self.piped: list[tuple[list[str], bytes]] = []


@pytest.fixture(autouse=True)
def board(monkeypatch) -> Clipboard:
    board = Clipboard()
    monkeypatch.setattr(App, "copy_to_clipboard", lambda self, text: board.osc52.append(text))

    def run(argv, **kwargs: Any):
        board.piped.append((list(argv), kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(clipboard.subprocess, "run", run)
    monkeypatch.setattr(clipboard.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    for name in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    return board


def notes(app) -> list[str]:
    assert not any(n.markup for n in app._notifications)
    return [n.message for n in app._notifications]


# --- P8: y copies the code block in view ---


async def test_y_copies_the_whole_code_block_in_view(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        fence = app.query("MarkdownFence").first()
        assert fence.max_scroll_x > 0  # the long line really is cut off on screen
        await pilot.press("y")
        assert board.osc52 == [CODE_1]
        assert board.piped == [(["/usr/bin/pbcopy"], CODE_1.encode())]
        assert notes(app) == [f"Copied code block to clipboard ({len(CODE_1)} chars)"]


async def test_y_takes_the_block_nearest_the_top_of_the_view(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        first, second = app.query("MarkdownFence")
        # Scroll the first block half out of view: it's still the nearest one to the top.
        viewer.scroll_to(y=first.virtual_region.y + 2, animate=False)
        await pilot.pause()
        await pilot.press("y")
        # ... and once it's gone, the next one is.
        viewer.scroll_to(y=first.virtual_region.bottom + 1, animate=False)
        await pilot.pause()
        await pilot.press("y")
        assert board.osc52 == [CODE_1, CODE_2]
        assert second.code == CODE_2


async def test_y_prefers_the_hovered_block(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        second = app.query("MarkdownFence").last()
        await pilot.hover(second, offset=(3, 1))
        await pilot.press("y")
        assert board.osc52 == [CODE_2]


async def test_y_without_a_code_block_in_view_says_so(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_end(animate=False)
        await pilot.pause()
        await pilot.press("y")
        assert board.osc52 == [] and board.piped == []
        assert notes(app) == ["No code block in view"]


# --- P8: Y copies the article ---


async def test_shift_y_copies_the_whole_article_as_markdown(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("Y")
        assert board.osc52 == [CODE_MD]
        assert notes(app) == [f"Copied article to clipboard ({len(CODE_MD)} chars)"]


async def test_shift_y_never_copies_terminal_escapes(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], "# Title\n\nsafe \x1b]52;c;ZXZpbA==\x07text")
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("Y")
        assert board.osc52 == ["# Title\n\nsafe text"]


async def test_shift_y_before_the_article_is_there_says_so(make_app, board):
    app = make_app()  # nothing cached, and every page 404s
    seed(app)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("Y")
        assert board.osc52 == []
        assert notes(app) == ["No article text to copy yet"]


async def test_copy_article_is_an_items_only_action(make_app):
    app = make_app()
    async with app.run_test(size=(130, 50)) as pilot:
        assert app.check_action("copy_article", ()) is True
        await pilot.press("2")
        assert app.check_action("copy_article", ()) is False


# --- P9: copy on select ---


async def drag_select(pilot, widget, start: tuple[int, int], end: tuple[int, int]) -> None:
    await pilot.mouse_down(widget, offset=start)
    await pilot.hover(widget, offset=end)
    await pilot.mouse_up(widget, offset=end)
    await pilot.pause()


SELECT_MD = "# Title\n\nThe quick brown fox jumps over the lazy dog.\n\n" + "word " * 200


async def test_selecting_text_copies_it(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], SELECT_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        paragraph = app.query("MarkdownParagraph").first()
        await drag_select(pilot, paragraph, (0, 0), (8, 0))  # the end cell is included
        await until(pilot, lambda: board.osc52 != [])
        assert board.osc52 == ["The quick"]
        assert board.piped == [(["/usr/bin/pbcopy"], b"The quick")]
        assert notes(app) == ["Copied to clipboard (9 chars)"]


async def test_a_plain_click_copies_nothing(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], SELECT_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await pilot.click(app.query("MarkdownParagraph").first(), offset=(3, 0))
        await pilot.pause()
        assert board.osc52 == [] and notes(app) == []


async def test_copy_on_select_can_be_turned_off(make_app, board):
    app = make_app(
        config=Config(scout=ScoutConfig(auto_after_hours=0), tui=TuiConfig(copy_on_select=False))
    )
    cache(app, seed(app)[0], SELECT_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await drag_select(pilot, app.query("MarkdownParagraph").first(), (0, 0), (8, 0))
        assert app.screen.get_selected_text() == "The quick"  # selecting itself still works
        assert board.osc52 == [] and notes(app) == []


async def test_over_ssh_a_selection_only_uses_osc52(make_app, board, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 5000 10.0.0.2 22")
    app = make_app()
    cache(app, seed(app)[0], SELECT_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await drag_select(pilot, app.query("MarkdownParagraph").first(), (0, 0), (8, 0))
        await until(pilot, lambda: board.osc52 != [])
        assert board.osc52 == ["The quick"] and board.piped == []
        assert notes(app) == [
            "Sent to the terminal clipboard (9 chars) — your terminal must allow clipboard access"
        ]


async def test_selecting_across_a_cut_off_code_line_copies_all_of_it(make_app, board):
    # Textual selects in the code's own text, not in screen cells: a line that runs past the
    # right edge is copied whole when the selection spans it.
    app = make_app()
    cache(app, seed(app)[0], CODE_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        fence = app.query("MarkdownFence").first()
        assert fence.max_scroll_x > 0
        code = fence.query_one("#code-content")  # padding 1 2: the text starts at (2, 1)
        await drag_select(pilot, code, (2, 1), (6, 3))
        await until(pilot, lambda: board.osc52 != [])
        assert board.osc52 == [f"import os\n{WIDE_LINE}\nprint"]


async def test_a_selection_is_copied_once_however_many_mouse_ups_follow(make_app, board):
    # TextSelected fires on every mouse-up, and a selection survives a scrollbar drag: without
    # a guard each drag re-copied it, silently replacing whatever was copied since.
    app = make_app()
    cache(app, seed(app)[0], SELECT_MD + "\n\n" + "\n\n".join(["more words " * 40] * 30))
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        await drag_select(pilot, app.query("MarkdownParagraph").first(), (0, 0), (8, 0))
        await until(pilot, lambda: board.osc52 != [])
        bar = app.query_one(ReaderPane).viewer.vertical_scrollbar
        for _ in range(2):
            await drag_select(pilot, bar, (0, 3), (0, 10))
        assert app.screen.get_selected_text() == "The quick"  # the selection is still there
        await drag_select(pilot, app.query_one("#items"), (5, 2), (12, 2))
        assert board.osc52 == ["The quick"]
        assert notes(app) == ["Copied to clipboard (9 chars)"]


async def test_a_new_selection_is_copied_again(make_app, board):
    app = make_app()
    cache(app, seed(app)[0], SELECT_MD)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        paragraph = app.query("MarkdownParagraph").first()
        await drag_select(pilot, paragraph, (0, 0), (8, 0))
        await until(pilot, lambda: len(board.osc52) == 1)
        await pilot.click(paragraph, offset=(20, 0))  # clears it
        await drag_select(pilot, paragraph, (0, 0), (8, 0))  # the same text, selected again
        await until(pilot, lambda: len(board.osc52) == 2)
        assert board.osc52 == ["The quick", "The quick"]
