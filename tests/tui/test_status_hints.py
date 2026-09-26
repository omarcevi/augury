"""The hints row shows whole hints only: one that doesn't fit is left out, not cut to "y:co…"."""

import re

import pytest

from augury.tui.keymap import KEYMAP
from augury.tui.widgets.status_line import _ESSENTIAL_LABELS, StatusLine
from tests.tui.test_reader import cache, open_first, seed


def shown_hints(row: str, mode: str) -> list[str]:
    assert row.lstrip().startswith(mode)
    return [piece for piece in re.split(r"\s{2,}", row.strip()[len(mode) :]) if piece]


@pytest.mark.parametrize("width", [80, 90, 130, 180])
@pytest.mark.parametrize("mode", ["NORMAL", "READ"])
async def test_the_hints_row_never_ends_in_half_a_hint(make_app, mode, width):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(width, 40)) as pilot:
        if mode == "READ":
            await open_first(pilot)
        await pilot.pause()
        status = app.query_one(StatusLine)
        row = status.render_line(1).text
        assert "…" not in row
        whole = {f"{hint.key}:{hint.label}" for hint in KEYMAP[mode]}
        shown = shown_hints(row, mode)
        assert shown and set(shown) <= whole
        essentials = {f"{h.key}:{h.label}" for h in KEYMAP[mode] if h.label in _ESSENTIAL_LABELS}
        assert essentials <= set(shown)


async def test_a_wider_terminal_shows_more_hints(make_app):
    app = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        narrow = shown_hints(app.query_one(StatusLine).render_line(1).text, "NORMAL")
        await pilot.resize_terminal(250, 40)
        await pilot.pause()
        wide = shown_hints(app.query_one(StatusLine).render_line(1).text, "NORMAL")
        assert len(wide) > len(narrow) and wide[: len(narrow)] == narrow
