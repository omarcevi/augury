"""P14: every setting on the config page (view 3) is editable there, defaults included."""

import tomllib

import pytest
from textual.widgets import Input, OptionList

from augury.core.config import Config, HttpConfig, ScoutConfig
from augury.tui.ui_state import UiState, load, save
from augury.tui.widgets.config_view import ConfigView, SettingRow, SettingsTable
from augury.tui.widgets.input_modal import InputModal
from augury.tui.widgets.picker_modal import ChoiceModal
from augury.tui.widgets.reader_pane import ReaderPane
from tests.helpers import NullHttp
from tests.tui.conftest import until

SIZE = (160, 40)


def file_toml(app) -> dict:
    return tomllib.loads(app.paths.config_file.read_text(encoding="utf-8"))


def table(app) -> SettingsTable:
    return app.query_one(ConfigView).table


def row(app, name: str) -> SettingRow:
    return next(r for r in table(app).settings if f"{r.section}.{r.field}" == name)


def source_cell(app, name: str) -> str:
    return str(table(app).get_row(name)[2])


def notes(app) -> list[str]:
    return [n.message for n in app._notifications]


async def select(pilot, name: str) -> None:
    app = pilot.app
    names = [f"{r.section}.{r.field}" for r in table(app).settings]
    table(app).move_cursor(row=names.index(name))
    await pilot.pause()


async def open_config(pilot) -> None:
    await pilot.press("3")
    await pilot.pause()


async def test_the_settings_table_is_focused_and_lists_every_setting_in_order(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        view = app.query_one(ConfigView)
        assert app.focused is view.table
        names = [f"{r.section}.{r.field}" for r in view.table.settings]
        assert names[:3] == [
            "scout.auto_after_hours",
            "scout.enrich_max_per_run",
            "scout.prefetch_top_n",
        ]
        assert "tui.tldr" in names and names[-1] == "pricing.*"
        assert view.table.row_count == len(names)
        assert source_cell(app, "tui.tldr") == "default"


@pytest.mark.parametrize("key", ["space", "enter"])
async def test_a_default_bool_flips_and_is_saved_to_config_toml(make_app, key):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        assert app.config.tui.copy_on_select is True
        await select(pilot, "tui.copy_on_select")
        await pilot.press(key)
        await pilot.pause()
        assert file_toml(app) == {"tui": {"copy_on_select": False}}
        assert app.config.tui.copy_on_select is False
        assert row(app, "tui.copy_on_select").source == "config.toml"
        assert source_cell(app, "tui.copy_on_select") == "config.toml"
        assert "Saved tui.copy_on_select = False · applies now" in notes(app)
        assert table(app).cursor_row == [r.field for r in table(app).settings].index(
            "copy_on_select"
        )  # the cursor stays on the row


async def test_a_literal_is_picked_from_its_values_and_applies_now(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.tldr")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        options = app.screen.query_one(OptionList)
        assert [options.get_option_at_index(i).id for i in range(options.option_count)] == [
            "shown",
            "collapsed",
        ]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert file_toml(app) == {"tui": {"tldr": "collapsed"}}
        assert app.config.tui.tldr == "collapsed"
        assert app.query_one(ReaderPane).tldr_collapsed is True  # the next article opens so
        assert "Saved tui.tldr = collapsed · applies now" in notes(app)


async def test_escape_in_the_picker_changes_nothing(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.tldr")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not app.paths.config_file.exists()
        assert isinstance(app.screen.query_one(ConfigView), ConfigView)


async def test_a_number_out_of_range_is_refused_in_the_modal_then_a_valid_one_saved(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.reading_width")
        await pilot.press("enter")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, InputModal)
        hint = str(modal.query_one("#input-hint").render())
        assert "40\N{EN DASH}200" in hint and "default 88" in hint
        field = modal.query_one(Input)
        assert field.value == "88"  # the current value
        field.value = "20"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is modal  # still open, saying why
        error = modal.query_one("#input-error")
        assert error.display and "greater than or equal to 40" in str(error.render())
        assert not app.paths.config_file.exists()
        field.value = "100"
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, InputModal)
        assert file_toml(app) == {"tui": {"reading_width": 100}}
        assert app.config.tui.reading_width == 100
        document = app.query_one(ReaderPane).viewer.document
        assert document.styles.max_width is not None
        assert document.styles.max_width.value == 100 + document.styles.gutter.width
        assert "Saved tui.reading_width = 100 · applies now" in notes(app)


async def test_an_edit_keeps_the_files_comments_and_other_keys(make_app):
    app = make_app()
    original = '# mine\n[export]\npath = ""  # off for now\n'
    app.paths.config_file.write_text(original, encoding="utf-8")
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.remember_state")
        await pilot.press("space")
        await pilot.pause()
        text = app.paths.config_file.read_text(encoding="utf-8")
        assert text.startswith(original)
        assert file_toml(app) == {"export": {"path": ""}, "tui": {"remember_state": False}}
        assert "Saved tui.remember_state = False · applies on next launch" in notes(app)


async def test_backspace_resets_a_config_toml_row_and_removes_an_emptied_section(make_app):
    app = make_app(config=Config(scout=ScoutConfig(auto_after_hours=0, prefetch_top_n=3)))
    app.paths.config_file.write_text(
        '[export]\npath = ""\n\n[scout]\nprefetch_top_n = 3\n', encoding="utf-8"
    )
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        assert source_cell(app, "scout.prefetch_top_n") == "config.toml"
        await select(pilot, "scout.prefetch_top_n")
        await pilot.press("backspace")
        await pilot.pause()
        assert file_toml(app) == {"export": {"path": ""}}
        assert app.config.scout.prefetch_top_n == 10
        assert row(app, "scout.prefetch_top_n").source == "default"
        assert "Reset scout.prefetch_top_n to its default: 10 · applies from the next scout" in (
            notes(app)
        )


async def test_backspace_on_a_default_row_does_nothing(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.tldr")
        await pilot.press("delete")
        await pilot.pause()
        assert not app.paths.config_file.exists()
        assert "tui.tldr is already the default" in notes(app)


async def test_a_malformed_config_toml_refuses_every_edit(make_app):
    app = make_app()
    app.paths.config_file.write_text("[tui\ntldr = 'x'\n", encoding="utf-8")
    before = app.paths.config_file.read_bytes()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        for name, key in (("tui.copy_on_select", "space"), ("tui.tldr", "enter")):
            await select(pilot, name)
            await pilot.press(key)
            await pilot.pause()
            assert not isinstance(app.screen, ChoiceModal | InputModal)
        await pilot.press("backspace")
        await pilot.pause()
        assert app.paths.config_file.read_bytes() == before
        refused = [n for n in app._notifications if "not valid TOML" in n.message]
        assert len(refused) == 3 and all("press e" in n.message and not n.markup for n in refused)


async def test_editing_the_theme_writes_config_toml_and_clears_the_last_used_one(make_app, paths):
    save(paths, UiState(theme="dracula"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        assert app.theme == "dracula"
        await select(pilot, "tui.theme")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        options = app.screen.query_one(OptionList)
        ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
        assert set(ids) == set(app.available_themes)
        options.highlighted = ids.index("nord")
        await pilot.press("enter")
        await pilot.pause()
        assert file_toml(app) == {"tui": {"theme": "nord"}}
        assert app.theme == "nord"
        assert load(paths).theme is None  # config.toml wins from now on
        assert row(app, "tui.theme").source == "config.toml"
        assert "Saved tui.theme = nord · applies now" in notes(app)
        await pilot.press("t")  # t keeps its own behaviour: last used, in ui_state.json
        await pilot.pause()
        assert load(paths).theme == app.theme != "nord"


async def test_resetting_the_theme_goes_back_to_the_default_everywhere(make_app, paths):
    paths.config_file.write_text('[tui]\ntheme = "nord"\n', encoding="utf-8")
    save(paths, UiState(theme="dracula"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "tui.theme")
        await pilot.press("backspace")
        await pilot.pause()
        assert not app.paths.config_file.read_text(encoding="utf-8").strip()
        assert load(paths).theme is None and app.theme == "textual-dark"


@pytest.mark.parametrize("name", ["models.overrides", "pricing.*"])
async def test_a_dict_setting_is_left_to_config_toml(make_app, name):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, name)
        for key in ("enter", "space", "backspace"):
            await pilot.press(key)
            await pilot.pause()
        assert not app.paths.config_file.exists()
        assert notes(app).count("Edit this one in config.toml (press e)") == 3


async def test_a_masked_setting_cannot_be_edited(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        view = app.query_one(ConfigView)
        view.table.show([SettingRow("google", "api_key", "••••••", "config.toml")])
        await pilot.pause()
        for key in ("enter", "backspace"):
            await pilot.press(key)
            await pilot.pause()
        assert not app.paths.config_file.exists()
        assert all("secret" in n for n in notes(app)) and len(notes(app)) == 2


async def test_a_text_setting_is_prefilled_and_validated_as_a_model_spec(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "models.fast")
        await pilot.press("enter")
        await pilot.pause()
        field = app.screen.query_one(Input)
        assert field.value == app.config.models.fast
        field.value = "no-provider"
        await pilot.press("enter")
        await pilot.pause()
        assert "provider/model" in str(app.screen.query_one("#input-error").render())
        field.value = "openai/gpt-x"
        await pilot.press("enter")
        await pilot.pause()
        assert file_toml(app) == {"models": {"fast": "openai/gpt-x"}}
        assert "Saved models.fast = openai/gpt-x · applies on next launch" in notes(app)


async def test_export_path_refuses_a_folder_whose_parent_is_missing(make_app, tmp_path):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "export.path")
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = str(tmp_path / "no" / "such")
        await pilot.press("enter")
        await pilot.pause()
        assert "neither is its parent" in str(app.screen.query_one("#input-error").render())
        app.screen.query_one(Input).value = str(tmp_path / "digests")
        await pilot.press("enter")
        await pilot.pause()
        assert file_toml(app) == {"export": {"path": str(tmp_path / "digests")}}
        assert f"Saved export.path = {tmp_path / 'digests'} · applies from the next scout" in (
            notes(app)
        )


async def test_an_http_change_reaches_the_next_scout(make_app, paths):
    built: list[HttpConfig] = []

    def factory(cfg: HttpConfig) -> NullHttp:
        built.append(cfg)
        return NullHttp()

    app = make_app()
    app.http_factory = factory
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "http.retries")
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "1"
        await pilot.press("enter")
        await pilot.pause()
        assert "Saved http.retries = 1 · applies from the next scout" in notes(app)
        assert [c.retries for c in built] == [3]  # not before the scout
        await pilot.press("r")
        await until(pilot, lambda: not app.scouting and len(built) == 2)
        assert built[-1].retries == 1


async def test_y_copies_the_whole_report(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        for key in ("y", "Y"):
            app._clipboard = ""
            await pilot.press(key)
            await pilot.pause()
            copied = app._clipboard
            assert copied == app.query_one(ConfigView).text_content
            for header in ("Settings", "Paths", "Sources", "Schedule", "Versions"):
                assert f"\n{header}\n" in f"\n{copied}\n"
            assert "tui.tldr = shown  (default)" in copied


async def test_the_edit_keys_do_nothing_outside_the_config_view(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        for action in ("edit_setting", "reset_setting", "copy_config", "edit_config"):
            assert app.check_action(action, ()) is False
        await pilot.press("space", "backspace", "delete", "y")
        await pilot.pause()
        await pilot.press("2", "space", "backspace")
        await pilot.pause()
        assert app.check_action("edit_setting", ()) is False
        assert not app.paths.config_file.exists()
        await pilot.press("3")
        assert app.check_action("edit_setting", ()) is not False


def in_view(app) -> bool:
    view, settings = app.query_one(ConfigView), table(app)
    y = settings.virtual_region.y + settings.header_height + settings.cursor_row
    return view.scroll_y <= y < view.scroll_y + view.scrollable_content_region.height


async def test_vim_keys_move_through_the_settings_and_keep_the_row_in_view(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await open_config(pilot)
        view = app.query_one(ConfigView)
        await pilot.press("j", "j")
        assert table(app).cursor_row == 2
        await pilot.press("k")
        assert table(app).cursor_row == 1
        await pilot.press("G")
        await pilot.pause()
        assert table(app).cursor_row == table(app).row_count - 1 and in_view(app)
        below = view.scroll_y
        await pilot.press("j", "j")  # past the last row: the page reads on below the table
        await pilot.pause()
        assert table(app).cursor_row == table(app).row_count - 1 and view.scroll_y == below + 2
        for _ in range(8):
            await pilot.press("k")
        await pilot.pause()
        assert in_view(app)
        await pilot.press("pageup")
        await pilot.pause()
        assert 0 < table(app).cursor_row < table(app).row_count - 9 and in_view(app)
        await pilot.press("g")
        await pilot.pause()
        assert table(app).cursor_row == 0 and view.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert table(app).cursor_row == view.scrollable_content_region.height - 2 and in_view(app)


async def test_values_are_shown_literally(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        app.query_one(ConfigView).table.show(
            [SettingRow("google", "project", "[bold]x[/bold]", "config.toml")]
        )
        await pilot.pause()
        assert str(table(app).get_row("google.project")[1]) == "[bold]x[/bold]"


async def test_help_lists_the_config_pages_keys(make_app):
    app = make_app()
    async with app.run_test(size=(180, 50)) as pilot:
        await open_config(pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        body = str(app.screen.query_one("#help-text").render())
        config = body[body.index("CONFIG") : body.index("NORMAL")]  # the current mode first
        for key in ("enter", "space", "⌫", "del", "e", "y", "g/G"):
            assert f"\n  {key} " in config


async def test_a_blank_value_reads_as_blank_in_the_toast(make_app):
    app = make_app()
    app.paths.config_file.write_text('[export]\npath = "/somewhere"\n', encoding="utf-8")
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "export.path")
        await pilot.press("backspace")
        await pilot.pause()
        assert "Reset export.path to its default: (blank) · applies from the next scout" in (
            notes(app)
        )
