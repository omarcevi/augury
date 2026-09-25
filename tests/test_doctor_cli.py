import httpx
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


def test_doctor_reports_database(paths):
    result = CliRunner().invoke(main, ["doctor", "--offline"])
    assert "✓ database" in result.output
    assert "(schema v1)" in result.output


def test_doctor_network_check(paths, respx_mock):
    paths.config_file.write_text("[http]\nmin_interval_s = 0\n")  # no real sleeping in tests
    respx_mock.get("https://huggingface.co/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(url__startswith="https://huggingface.co/api/daily_papers").mock(
        return_value=httpx.Response(200, json=[{"paper": {"id": "1", "title": "t"}}])
    )
    result = CliRunner().invoke(main, ["doctor"])
    assert "✓ network" in result.output
