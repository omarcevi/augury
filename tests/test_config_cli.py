import pytest
from click.testing import CliRunner

from augury.cli import main
from augury.tui.ui_state import UiState, save


def test_config_path_prints_only_the_config_file_path(paths):
    result = CliRunner().invoke(main, ["config", "--path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(paths.config_file)


def test_config_prints_every_section_with_defaults(paths):
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 0, result.output
    for header in ("Settings", "Paths", "Sources", "Schedule", "Versions"):
        assert f"\n{header}\n" in f"\n{result.output}\n"
    assert "tui.theme = textual-dark  (default)" in result.output
    assert str(paths.config_file) in result.output
    assert "3 enabled, 0 disabled" in result.output  # the 3 builtin sources, seeded on open


def test_config_labels_a_value_set_in_config_toml(paths):
    paths.config_file.write_text("[scout]\nauto_after_hours = 6\n")
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 0, result.output
    assert "scout.auto_after_hours = 6.0  (config.toml)" in result.output


def test_config_reports_an_invalid_config_toml(paths):
    paths.config_file.write_text("[scout\n")
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code != 0
    assert "not valid TOML" in result.output


def theme_line(output: str) -> str:
    return next(line.strip() for line in output.splitlines() if "tui.theme =" in line)


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ("nord", "tui.theme = nord  (last used, ui_state.json; config.toml: dracula)"),
        (None, "tui.theme = dracula  (config.toml)"),
        ("no-such-theme", "tui.theme = dracula  (config.toml)"),
    ],
)
def test_config_shows_the_theme_the_app_runs_with(paths, saved, expected):
    # The theme picked with `t` (data/ui_state.json) wins over config.toml's, as in the app.
    paths.config_file.write_text('[tui]\ntheme = "dracula"\n')
    if saved:
        save(paths, UiState(theme=saved))
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 0, result.output
    assert theme_line(result.output) == expected
