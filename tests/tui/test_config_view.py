from pydantic import BaseModel, ConfigDict, Field

from augury.agents.normalize import store_items
from augury.core.config import Config, ScoutConfig
from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourcesRepo
from augury.core.models import RawItem
from augury.tui.widgets.config_view import (
    build_config_report,
    editor_command,
    effective_settings,
    render_config_text,
)
from tests.tui.conftest import NOW


def test_effective_settings_labels_config_toml_vs_default():
    config = Config(scout=ScoutConfig(auto_after_hours=6.0))
    raw = {"scout": {"auto_after_hours": 6.0}}
    rows = {(r.section, r.field): r for r in effective_settings(config, raw)}
    assert rows[("scout", "auto_after_hours")].source == "config.toml"
    assert rows[("scout", "auto_after_hours")].value == 6.0
    # A field the user never set in config.toml is still listed, just labelled "default".
    assert rows[("scout", "prefetch_top_n")].source == "default"
    assert rows[("scout", "prefetch_top_n")].value == 10


def test_effective_settings_labels_a_value_equal_to_the_default_as_config_toml():
    # The source must come only from "is this key present in the file", never from
    # "does the value differ from the pydantic default" -- a user who wrote the default
    # value explicitly still gets `config.toml`, not `default`.
    default = ScoutConfig().auto_after_hours
    config = Config(scout=ScoutConfig(auto_after_hours=default))
    raw = {"scout": {"auto_after_hours": default}}
    rows = {(r.section, r.field): r for r in effective_settings(config, raw)}
    row = rows[("scout", "auto_after_hours")]
    assert row.value == default and row.source == "config.toml"


def test_effective_settings_ignores_a_raw_section_that_is_not_a_table():
    # A malformed or unrelated raw TOML shape (not the {field: value} table this section
    # expects) must never crash the renderer -- every field just falls back to "default".
    config = Config()
    rows = {(r.section, r.field): r for r in effective_settings(config, {"tui": "not-a-table"})}
    assert rows[("tui", "theme")].source == "default"


def test_effective_settings_picks_up_a_field_no_code_here_has_ever_heard_of():
    # Proves the renderer is generic: it walks `type(config).model_fields`, so a field
    # added to TuiConfig (or any section) by a parallel lane needs no change here.
    class _Section(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class FutureTuiConfig(_Section):
        theme: str = "textual-dark"
        reading_width: int = 88  # stands in for a field another lane is adding

    class FutureConfig(_Section):
        tui: FutureTuiConfig = Field(default_factory=FutureTuiConfig)

    raw = {"tui": {"reading_width": 100}}
    # As in real use (`Config.model_validate(load_raw_toml(paths))`), the config object
    # already reflects the file; `effective_settings` only reads it and labels the source.
    rows = effective_settings(FutureConfig.model_validate(raw), raw)
    by_field = {(r.section, r.field): r for r in rows}
    assert by_field[("tui", "reading_width")].value == 100
    assert by_field[("tui", "reading_width")].source == "config.toml"
    assert by_field[("tui", "theme")].source == "default"


def test_build_config_report_paths_versions_and_source_counts(paths):
    conn = open_db(paths, now=NOW)
    SourcesRepo(conn).set_enabled("hf-community", False)
    store_items(conn, [RawItem(source_id="hf-blog", url="https://x/a", title="A")], now=NOW)
    config = Config()
    report = build_config_report(conn, config, paths, {})

    path_labels = dict(report.paths)
    assert path_labels["config.toml"] == str(paths.config_file)
    assert path_labels["data dir"] == str(paths.data_dir)
    # Lane 1's AppPaths.ui_state_file doesn't exist in this worktree yet: fall back to the
    # same computed path so the row is still correct once it's merged in.
    assert path_labels["ui state"] == str(paths.data_dir / "ui_state.json")

    assert report.sources_enabled == 2 and report.sources_disabled == 1
    assert report.sources_health.get("never") == 3  # nothing has been scouted yet

    version_labels = dict(report.versions)
    assert set(version_labels) == {"augury", "Python", "Textual", "sqlite-vec", "SQLite"}
    assert version_labels["SQLite"]


def test_build_config_report_export_path_shows_off_when_unset(paths):
    # The friendly "off" label is a Paths-section presentation choice (per the plan); the
    # Settings section still shows the raw, generic pydantic value ("").
    conn = open_db(paths, now=NOW)
    report = build_config_report(conn, Config(), paths, {})
    assert dict(report.paths)["export path"] == "off"
    settings_by_field = {(r.section, r.field): r for r in report.settings}
    assert settings_by_field[("export", "path")].value == ""


def test_render_config_text_has_every_section_header(paths):
    conn = open_db(paths, now=NOW)
    report = build_config_report(conn, Config(), paths, {})
    rendered = render_config_text(report)
    for header in ("Settings", "Paths", "Sources", "Schedule", "Versions"):
        assert f"\n{header}\n" in f"\n{rendered}\n"
    assert "tui.theme" in rendered and "(default)" in rendered


def test_editor_command_prefers_editor_then_visual_then_a_platform_opener(monkeypatch, tmp_path):
    target = tmp_path / "config.toml"
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "vim -O")
    assert editor_command(target) == ["vim", "-O", str(target)]

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "code --wait")
    assert editor_command(target) == ["code", "--wait", str(target)]

    monkeypatch.delenv("VISUAL", raising=False)
    cmd = editor_command(target)
    assert cmd[-1] == str(target) and cmd[0] in ("open", "xdg-open")
