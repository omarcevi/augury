from click.testing import CliRunner

from augury import __version__
from augury.cli import main


def test_version_flag_prints_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"augury, version {__version__}"


def test_help_describes_the_program():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "terminal-native AI digest reader" in result.output
