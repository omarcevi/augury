import httpx
from click.testing import CliRunner

from augury.cli import main
from augury.core.db.migrate import available_migrations


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


def test_doctor_reports_database(paths):
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "✓ database" in result.output
    assert f"(schema v{len(available_migrations())})" in result.output


def test_doctor_network_check(paths, respx_mock):
    paths.config_file.write_text("[http]\nmin_interval_s = 0\n")  # no real sleeping in tests
    respx_mock.get("https://huggingface.co/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(url__startswith="https://huggingface.co/api/daily_papers").mock(
        return_value=httpx.Response(200, json=[{"paper": {"id": "1", "title": "t"}}])
    )
    result = CliRunner().invoke(main, ["doctor"])
    assert "✓ network" in result.output


def test_doctor_offline_says_the_network_is_unreachable(paths, respx_mock):
    paths.config_file.write_text("[http]\nmin_interval_s = 0\nretries = 0\n")
    respx_mock.get("https://huggingface.co/robots.txt").mock(side_effect=httpx.ConnectError("x"))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1
    line = next(line for line in result.output.splitlines() if "network" in line)
    assert line.startswith("✗ network") and "network unreachable" in line
    assert "robots" not in line.split("unreachable")[0] and "disallowed" not in line


def test_doctor_without_a_key_explains_how_to_add_one(paths):
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert result.exit_code == 0, result.output  # AI is optional: a missing key isn't a failure
    assert "! fast" in result.output and "GEMINI_API_KEY" in result.output


def test_doctor_flags_an_env_file_others_can_read(paths):
    paths.env_file.write_text("GEMINI_API_KEY=secret-value\n")
    paths.env_file.chmod(0o644)
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "! .env" in result.output and "chmod 600" in result.output
    assert "secret-value" not in result.output
    assert "✓ fast" in result.output and "key found" in result.output


def test_doctor_warns_when_a_model_has_no_known_price(paths):
    paths.config_file.write_text('[models]\nfast = "gemini/gemini-future"\n')
    paths.env_file.write_text("GEMINI_API_KEY=x\n")
    paths.env_file.chmod(0o600)
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "! pricing" in result.output and "gemini/gemini-future" in result.output
    assert "! .env" not in result.output
