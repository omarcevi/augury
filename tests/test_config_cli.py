from click.testing import CliRunner

from augury.cli import main


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
