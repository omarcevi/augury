import pytest
from pydantic import BaseModel, ConfigDict, Field

from augury.agents.normalize import store_items
from augury.core.config import Config, PriceConfig, ScoutConfig, TuiConfig
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


def test_effective_settings_masks_a_secret_looking_field():
    # Config never actually holds a key (keys live in the environment or .env) -- this is a
    # tripwire, so a field added to some future section that looks like one is never echoed.
    class _Section(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class StandInSection(_Section):
        api_key: str = "sk-live-123"
        daily_tokens: int = 40  # must NOT be treated as a secret

    class StandInConfig(_Section):
        creds: StandInSection = Field(default_factory=StandInSection)

    rows = {r.field: r for r in effective_settings(StandInConfig(), {})}
    assert rows["api_key"].value == "••••••"
    assert rows["daily_tokens"].value == 40


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


def test_effective_settings_lists_a_configured_pricing_entry():
    # Config.pricing is a plain dict[str, PriceConfig], not a _Section -- the generic walk
    # must special-case it instead of crashing on `type(section).model_fields`.
    config = Config(pricing={"openai/x": PriceConfig(input_per_mtok=1, output_per_mtok=2)})
    raw = {"pricing": {"openai/x": {}}}
    rows = {(r.section, r.field): r for r in effective_settings(config, raw)}
    assert ("pricing", '"openai/x"') in rows


def test_render_config_text_shows_no_pricing_by_default(paths):
    conn = open_db(paths, now=NOW)
    report = build_config_report(conn, Config(), paths, {}, saved_theme=None)
    assert "pricing.* = none" in render_config_text(report)


def test_build_config_report_paths_versions_and_source_counts(paths):
    conn = open_db(paths, now=NOW)
    SourcesRepo(conn).set_enabled("hf-community", False)
    store_items(conn, [RawItem(source_id="hf-blog", url="https://x/a", title="A")], now=NOW)
    config = Config()
    report = build_config_report(conn, config, paths, {}, saved_theme=None)

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
    report = build_config_report(conn, Config(), paths, {}, saved_theme=None)
    assert dict(report.paths)["export path"] == "off"
    settings_by_field = {(r.section, r.field): r for r in report.settings}
    assert settings_by_field[("export", "path")].value == ""


def test_render_config_text_has_every_section_header(paths):
    conn = open_db(paths, now=NOW)
    report = build_config_report(conn, Config(), paths, {}, saved_theme=None)
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


def theme_line(rendered: str) -> str:
    return next(line.strip() for line in rendered.splitlines() if "tui.theme =" in line)


@pytest.mark.parametrize(
    ("saved", "configured", "expected"),
    [
        # A theme picked with `t` overrides config.toml's, and the page says both.
        ("nord", "dracula", "tui.theme = nord  (last used, ui_state.json; config.toml: dracula)"),
        ("nord", None, "tui.theme = nord  (last used, ui_state.json)"),
        (None, "dracula", "tui.theme = dracula  (config.toml)"),
        (None, None, "tui.theme = textual-dark  (default)"),
        # A saved theme that's no longer available falls back, as at launch.
        ("no-such-theme", "dracula", "tui.theme = dracula  (config.toml)"),
        ("no-such-theme", None, "tui.theme = textual-dark  (default)"),
        (None, "nor-this", "tui.theme = textual-dark  (default; config.toml: nor-this)"),
    ],
)
def test_the_theme_row_shows_the_running_theme_and_where_it_came_from(
    paths, saved, configured, expected
):
    conn = open_db(paths, now=NOW)
    raw = {"tui": {"theme": configured}} if configured else {}
    config = Config(tui=TuiConfig(theme=configured)) if configured else Config()
    report = build_config_report(conn, config, paths, raw, saved_theme=saved)
    assert theme_line(render_config_text(report)) == expected


@pytest.mark.parametrize(
    ("running", "configured", "saved", "expected"),
    [
        # `e` changed config.toml since launch; the app keeps the theme it started with.
        (
            "textual-dark",
            "dracula",
            None,
            "tui.theme = textual-dark  (running; config.toml: dracula — applies on next launch)",
        ),
        (
            "nord",
            "nor-this",
            None,
            "tui.theme = nord  (running; config.toml: nor-this isn't available"
            " — textual-dark on next launch)",
        ),
        ("dracula", None, None, "tui.theme = dracula  (running; textual-dark on next launch)"),
        # Running what it resolves to: nothing to add.
        (
            "nord",
            "dracula",
            "nord",
            "tui.theme = nord  (last used, ui_state.json; config.toml: dracula)",
        ),
        ("dracula", "dracula", None, "tui.theme = dracula  (config.toml)"),
    ],
)
def test_the_theme_row_says_when_the_running_theme_is_not_next_launchs(
    paths, running, configured, saved, expected
):
    conn = open_db(paths, now=NOW)
    raw = {"tui": {"theme": configured}} if configured else {}
    config = Config(tui=TuiConfig(theme=configured)) if configured else Config()
    report = build_config_report(conn, config, paths, raw, saved_theme=saved, running_theme=running)
    assert theme_line(render_config_text(report)) == expected
