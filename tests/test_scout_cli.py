import json
from pathlib import Path

import httpx
from click.testing import CliRunner

from augury.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "hf"


def test_scout_command_prints_a_report(paths, respx_mock):
    paths.config_file.write_text("[http]\nmin_interval_s = 0\n")  # no real sleeping in tests
    respx_mock.get("https://huggingface.co/robots.txt").mock(return_value=httpx.Response(404))
    for prefix, name in (
        ("/api/daily_papers", "daily_papers.json"),
        ("/api/blog/community", "community.json"),
        ("/api/blog", "blog.json"),
    ):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        respx_mock.get(url__startswith=f"https://huggingface.co{prefix}").mock(
            return_value=httpx.Response(200, json=payload)
        )
    result = CliRunner().invoke(main, ["scout"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Scout ok")
    assert "hf-papers" in result.output and "new" in result.output
