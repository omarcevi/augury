from datetime import timedelta

from augury.agents.normalize import store_items
from augury.core.db.runs_repo import RunsRepo
from augury.core.models import RawItem
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.status_line import StatusLine
from tests.tui.conftest import NOW
from tests.tui.test_items_table import CJK


async def test_health_bar_before_any_scout(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)):
        line = app.query_one(HealthBar).render().plain
        assert "Scout: never" in line and "Sources: 3 ✓" in line and "AI: not configured" in line


async def test_health_bar_after_a_scout(make_app):
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW - timedelta(hours=2))
    runs.finish(run_id, "ok", now=NOW - timedelta(hours=2))
    async with app.run_test(size=(120, 30)):
        assert "Scout: 07:00 (2h ago)" in app.query_one(HealthBar).render().plain


async def test_status_line_shows_mode_and_hints(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)):
        line = app.query_one(StatusLine).render().plain
        assert "NORMAL" in line and "?:help" in line and "q:quit" in line


async def test_status_line_keeps_the_hints_row_with_a_long_selected_title(make_app):
    app = make_app()
    store_items(app.conn, [RawItem(source_id="hf-blog", url="https://x/a", title=CJK)], now=NOW)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        hints_row = app.query_one(StatusLine).render_line(1).text
        assert "NORMAL" in hints_row and "q:quit" in hints_row


async def test_status_line_shows_essentials_at_80_columns(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        hints_row = app.query_one(StatusLine).render_line(1).text
        assert "?:help" in hints_row and "q:quit" in hints_row


async def test_help_overlay_opens_and_closes(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpOverlay)
        await pilot.press("escape")
        assert not isinstance(app.screen, HelpOverlay)


async def test_t_cycles_the_theme(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        before = app.theme
        await pilot.press("t")
        assert app.theme != before


def test_shell_snapshot(make_app, snap_compare):
    assert snap_compare(make_app(), terminal_size=(120, 30))
