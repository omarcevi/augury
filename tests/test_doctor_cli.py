from click.testing import CliRunner

from augury.cli import main


def test_doctor_passes_with_defaults(paths):
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code == 0, result.output
    assert "✓ config" in result.output
    assert "ok (defaults, no file yet)" in result.output


def test_doctor_fails_on_broken_config(paths):
    paths.config_file.write_text("[scout\n")
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code == 1
    assert "✗ config" in result.output
