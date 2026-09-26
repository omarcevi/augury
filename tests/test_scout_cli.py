import json
from pathlib import Path

import httpx
from click.testing import CliRunner

from augury.agents import scout
from augury.agents.scout import ScoutReport, SourceStats
from augury.agents.triage import TriageStats
from augury.cli import format_report, main

FIXTURES = Path(__file__).parent / "fixtures" / "hf"


def _serve_hf(paths, respx_mock) -> None:
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


def test_scout_command_prints_a_report(paths, respx_mock):
    _serve_hf(paths, respx_mock)
    respx_mock.get(url__startswith="https://huggingface.co/blog/").mock(
        return_value=httpx.Response(404)
    )
    result = CliRunner().invoke(main, ["scout"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Scout ok")
    assert "hf-papers" in result.output and "new" in result.output
    assert "enrichment" not in result.output


def test_scout_command_reports_a_failed_enrichment(paths, respx_mock, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("enrich exploded")

    monkeypatch.setattr(scout, "enrich_new_articles", boom)
    _serve_hf(paths, respx_mock)
    result = CliRunner().invoke(main, ["scout"])
    assert result.exit_code == 0, result.output  # the items were stored
    header, *lines = result.output.splitlines()
    assert header.startswith("Scout ok") and header.endswith("· enrichment failed")
    assert any(
        line.startswith("  ! enrichment") and "RuntimeError: enrich exploded" in line
        for line in lines
    )


def _report(triage: TriageStats) -> ScoutReport:
    return ScoutReport(
        run_id="r",
        status="ok",
        new_items=3,
        triage=triage,
        sources={"hf-blog": SourceStats(fetched=3, new=3)},
    )


def test_the_report_has_no_ai_line_without_a_key():
    text = format_report(
        _report(
            TriageStats(attempted=3, degraded="AI not configured", degraded_kind="not_configured")
        )
    )
    assert "triage" not in text  # exactly the M1 report


def test_the_report_shows_triage_and_degradation():
    ok = format_report(_report(TriageStats(attempted=3, triaged=3, hidden=1)))
    assert "✓ triage" in ok and "3 items · 1 hidden by triage" in ok
    budget = TriageStats(
        attempted=3, triaged=1, degraded="daily budget of $1.00 reached", degraded_kind="budget"
    )
    text = format_report(_report(budget))
    assert "⚠ triage" in text and "1/3 items · ranking degraded: daily budget" in text
