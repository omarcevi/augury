from datetime import timedelta

from textual.containers import VerticalScroll
from textual.widgets import Static

from augury.agents.normalize import store_items
from augury.core.db.runs_repo import RunsRepo
from augury.core.models import RawItem
from augury.llm.probes import ProbeResult, save_probe_results
from augury.tui.app import AuguryApp
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.status_line import StatusLine
from tests.helpers import ScriptedLlm, fake_resolver
from tests.tui.conftest import NOW, until
from tests.tui.test_items_table import CJK


async def test_health_bar_before_any_scout(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)):
        line = app.query_one(HealthBar).render().plain
        assert "Scout: never" in line and "Sources: 3 ✓" in line and "AI: not configured" in line


async def test_health_bar_shows_each_configured_role(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    async with app.run_test(size=(140, 30)):
        line = app.query_one(HealthBar).render().plain
        assert "fast: fake ✓" in line and "smart: fake ✓" in line
        assert "not configured" not in line


async def test_health_bar_after_a_scout(make_app):
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW - timedelta(hours=2))
    runs.finish(run_id, "ok", now=NOW - timedelta(hours=2))
    async with app.run_test(size=(120, 30)):
        assert "Scout: 07:00 (2h ago)" in app.query_one(HealthBar).render().plain


async def test_health_bar_keeps_the_scout_age_current(make_app, monkeypatch):
    monkeypatch.setattr(AuguryApp, "HEALTH_REFRESH_S", 0.01)
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW - timedelta(hours=2))
    runs.finish(run_id, "ok", now=NOW - timedelta(hours=2))
    clock = [NOW]
    app.now = lambda: clock[0]
    async with app.run_test(size=(120, 30)) as pilot:
        bar = app.query_one(HealthBar)
        assert "(2h ago)" in bar.render().plain
        clock[0] = NOW + timedelta(hours=1)
        await until(pilot, lambda: "(3h ago)" in bar.render().plain)


async def test_health_bar_warns_when_enrichment_failed(make_app):
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW - timedelta(hours=2))
    stats = {"enrich_error": "1 of 3 failed; first: web:x: RuntimeError: boom"}
    runs.finish(run_id, "ok", now=NOW - timedelta(hours=2), stats=stats)
    async with app.run_test(size=(120, 30)):
        line = app.query_one(HealthBar).render()
        assert "Scout: 07:00 (2h ago) · enrich ⚠" in line.plain
        warning = app.get_css_variables()["warning"]
        assert any(str(span.style) == warning for span in line.spans)


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


def visible_help(app) -> list[str]:
    scroll = app.screen.query_one("#help-body", VerticalScroll)
    lines = str(app.screen.query_one("#help-text", Static).render()).splitlines()
    top = round(scroll.scroll_y)
    return lines[top : top + scroll.scrollable_content_region.height]


async def test_help_fits_80x24_and_scrolls_to_every_mode(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        shown = visible_help(app)
        assert shown[0].strip() == "NORMAL" and any(line.split() == ["q", "quit"] for line in shown)
        assert not any("SOURCES" in line for line in shown)
        await pilot.press("end")
        await pilot.pause()
        shown = visible_help(app)
        assert any("CONFIG" in line for line in shown)  # the last mode (P14 made it longer)
        assert any(line.split() == ["enter", "edit"] for line in shown)
        for _ in range(30):  # back up to the one before it
            if any("SOURCES" in line for line in visible_help(app)):
                break
            await pilot.press("k")
            await pilot.pause()
        shown = visible_help(app)
        assert any("SOURCES" in line for line in shown)
        assert any(line.split() == ["t", "test", "fetch"] for line in shown)


async def test_help_closes_with_escape_q_or_question_mark(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        for key in ("escape", "q", "question_mark"):
            await pilot.press("question_mark")
            assert isinstance(app.screen, HelpOverlay)
            await pilot.press(key)
            assert not isinstance(app.screen, HelpOverlay) and app.is_running


async def test_help_lists_the_current_modes_keys_first(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("2", "question_mark")
        await pilot.pause()
        assert visible_help(app)[0].strip() == "SOURCES"


async def test_t_cycles_the_theme(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)) as pilot:
        before = app.theme
        await pilot.press("t")
        assert app.theme != before


def test_shell_snapshot(make_app, snap_compare):
    assert snap_compare(make_app(), terminal_size=(120, 30))


async def test_health_bar_shows_todays_spend(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    runs = RunsRepo(app.conn)
    run_id = runs.start("summarize", now=NOW)
    runs.add_usage(run_id, tokens_in=10, tokens_out=10, cost_usd=0.034, unpriced_tokens=0)
    async with app.run_test(size=(160, 30)):
        assert "Today: $0.03" in app.query_one(HealthBar).render().plain


async def test_health_bar_says_when_the_budget_is_reached(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    runs = RunsRepo(app.conn)
    run_id = runs.start("summarize", now=NOW)
    runs.add_usage(run_id, tokens_in=1, tokens_out=1, cost_usd=1.5, unpriced_tokens=2500)
    async with app.run_test(size=(160, 30)):
        line = app.query_one(HealthBar).render().plain
        assert "Today: $1.50 + 2.5k tok unpriced" in line and "budget reached" in line


async def test_no_spend_segment_without_ai(make_app):
    app = make_app()
    async with app.run_test(size=(160, 30)):
        assert "Today:" not in app.query_one(HealthBar).render().plain


async def test_the_health_bar_shows_doctor_probe_results(make_app, paths):
    save_probe_results(
        paths.probe_cache_file,
        [
            ProbeResult("fast", "fake/fake-model", True, False, "no tool call", NOW),
            ProbeResult("smart", "fake/fake-model", False, False, "bad JSON", NOW),
        ],
    )
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    async with app.run_test(size=(140, 30)):
        line = app.query_one(HealthBar).render().plain
        assert "fast: fake ⚠" in line and "smart: fake ✗" in line
