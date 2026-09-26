from textual.widgets import ContentSwitcher

from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.tui.widgets.config_view import ConfigView
from augury.tui.widgets.confirm_modal import ConfirmModal
from augury.tui.widgets.status_line import StatusLine
from tests.tui.conftest import NOW


def seed_item(app, *, source_id: str = "hf-blog") -> str:
    return store_items(
        app.conn, [RawItem(source_id=source_id, url="https://x/a", title="A")], now=NOW
    ).new_ids[0]


async def test_switching_to_config_and_back_keeps_the_reader_open(make_app):
    # Mirrors test_switching_to_sources_and_back_keeps_the_reader_open: the Config view
    # (like Sources) merely hides the reader, it must never discard reading_id.
    app = make_app()
    item_id = seed_item(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.open_item(item_id)
        await pilot.pause()
        assert app.reading_id == item_id

        await pilot.press("3")
        assert app.reading_id == item_id  # untouched while Config is showing

        await pilot.press("1")
        await pilot.pause()
        assert app.reading_id == item_id  # intact, not discarded
        assert app.mode == "READ"
        assert app.screen.has_class("reading")
        await app.workers.wait_for_complete()


async def test_pressing_3_shows_every_config_section(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3")
        assert app.query_one(ContentSwitcher).current == "config-view"
        content = app.query_one(ConfigView).text_content
        for header in ("Settings", "Paths", "Sources", "Schedule", "Versions"):
            assert f"\n{header}\n" in f"\n{content}\n"
        assert "scout.auto_after_hours" in content
        assert "config.toml" in content  # the "config.toml" path row, at least


async def test_pressing_1_from_config_goes_back_to_items(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "1")
        assert app.query_one(ContentSwitcher).current == "main"
        assert app.mode == "NORMAL"


async def test_escape_from_config_goes_back_to_items(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "escape")
        assert app.query_one(ContentSwitcher).current == "main"


async def test_items_only_actions_are_disabled_in_config_view(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3")
        assert app.check_action("toggle_like", ()) is False
        assert app.check_action("pick_sources", ()) is False
        assert app.check_action("cycle_show", ()) is False
        assert app.check_action("help", ()) is not False  # unrelated actions stay enabled


async def test_config_hints_are_pinned_at_80_columns(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        hints_row = app.query_one(StatusLine).render_line(1).text
        assert all(hint in hints_row for hint in ("1:items", "esc:back", "?:help", "q:quit"))


async def test_e_opens_the_editor_and_reloads_a_valid_config(make_app):
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> None:
        calls.append(cmd)

    app = make_app(editor_runner=fake_runner)
    app.paths.config_file.write_text("[scout]\nauto_after_hours = 6\n")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "e")
        await pilot.pause()
        assert calls and calls[0][-1] == str(app.paths.config_file)
        assert app.config.scout.auto_after_hours == 6
        notes = [n.message for n in app._notifications]
        assert any("reloaded" in n for n in notes)
        # The view re-renders with the freshly reloaded value.
        assert "auto_after_hours = 6.0" in app.query_one(ConfigView).text_content


async def test_e_shows_a_clear_error_for_an_invalid_config_after_editing(make_app):
    def break_it(_cmd: list[str]) -> None:
        app.paths.config_file.write_text("[scout\n")  # invalid TOML

    app = make_app(editor_runner=break_it)
    app.paths.config_file.write_text("[scout]\nauto_after_hours = 6\n")
    previous = app.config
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "e")
        await pilot.pause()
        notes = [n for n in app._notifications if "not valid TOML" in n.message]
        assert notes and notes[0].severity == "error" and not notes[0].markup
        assert app.config is previous  # never crashed, never adopted the broken file
        assert app.is_running


async def test_e_offers_to_create_config_toml_when_missing_and_declining_creates_nothing(
    make_app,
):
    calls: list[list[str]] = []
    app = make_app(editor_runner=lambda cmd: calls.append(cmd))
    assert not app.paths.config_file.exists()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "e")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")
        await pilot.pause()
        assert not app.paths.config_file.exists()
        assert not calls


async def test_e_creates_config_toml_on_confirmation_then_opens_it(make_app):
    calls: list[list[str]] = []
    app = make_app(editor_runner=lambda cmd: calls.append(cmd))
    assert not app.paths.config_file.exists()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "e")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.paths.config_file.exists()
        assert calls and calls[0][-1] == str(app.paths.config_file)


async def test_e_shows_a_clear_error_when_the_editor_fails_to_launch(make_app):
    # A stale/misspelled $EDITOR, or a missing xdg-open on Linux, makes subprocess.run
    # raise (FileNotFoundError, typically) -- that must never crash the app, and it must
    # never escape `with self.suspend():` either, or the terminal is left suspended.
    def boom(cmd: list[str]) -> None:
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    app = make_app(editor_runner=boom)
    app.paths.config_file.write_text("[scout]\nauto_after_hours = 6\n")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("3", "e")
        await pilot.pause()
        notes = [n for n in app._notifications if "Couldn't open the editor" in n.message]
        assert notes and notes[0].severity == "error" and not notes[0].markup
        assert str(app.paths.config_file) in notes[0].message  # names the command tried
        assert app.is_running
        # No reload was attempted (the editor never actually ran), and the app stays
        # responsive to further input.
        assert not any("reloaded" in n.message for n in app._notifications)
        await pilot.press("1")
        assert app.mode == "NORMAL"


async def test_e_from_sources_view_still_toggles_enabled_not_the_editor(make_app):
    # "e" is Sources view's own enable/disable key (shadows the app-level editor action
    # while that view has focus, the same way "t" already does for theme vs. test-fetch).
    calls: list[list[str]] = []
    app = make_app(editor_runner=lambda cmd: calls.append(cmd))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "e")
        await pilot.pause()
        assert not calls
