import os

from click.testing import CliRunner

from augury.cli import main
from augury.core.secrets import load_env_file, read_env_file


def test_offline_tests_never_see_provider_keys():
    leaked = [
        k for k in os.environ if k.endswith("_API_KEY") or k.startswith(("GOOGLE_", "GEMINI_"))
    ]
    assert leaked == []


def test_env_file_fills_only_missing_variables(paths):
    paths.env_file.write_text("GEMINI_API_KEY=from-file\nOTHER=1\n")
    environ = {"OTHER": "from-shell"}
    assert load_env_file(paths, environ) == ["GEMINI_API_KEY"]
    assert environ == {"OTHER": "from-shell", "GEMINI_API_KEY": "from-file"}


def test_env_file_syntax(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\n\nexport A=1\nB = 'two words'\nC=\"x#y\"\nnot a line\n")
    assert read_env_file(env) == {"A": "1", "B": "two words", "C": "x#y"}


def test_a_missing_env_file_loads_nothing(paths):
    assert load_env_file(paths, {}) == []


def test_the_cli_loads_the_env_file_before_any_command(paths):
    paths.env_file.write_text("GEMINI_API_KEY=from-file\n")
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("GEMINI_API_KEY") == "from-file"
    assert "from-file" not in result.output  # keys are never printed
