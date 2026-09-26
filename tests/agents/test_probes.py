from datetime import UTC, datetime

from google.genai import types

from augury.core.config import Config
from augury.llm.probes import (
    ProbeResult,
    apply_probes,
    load_probe_results,
    probe_role,
    save_probe_results,
)
from augury.llm.resolver import role_statuses
from tests.helpers import ScriptedLlm, fake_resolver

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
STRUCTURED = '{"ok": true, "word": "augury"}'
TOOL_CALL = types.Part(
    function_call=types.FunctionCall(name="record_word", args={"word": "augury"})
)


async def probe(llm: ScriptedLlm) -> ProbeResult:
    return await probe_role("fast", config=Config(), resolver=fake_resolver(llm), now=lambda: NOW)


async def test_a_capable_model_passes_both_probes():
    result = await probe(ScriptedLlm(replies=[STRUCTURED, TOOL_CALL, "done"]))
    assert result.structured and result.tools and result.ok and result.detail == ""


async def test_a_model_without_structured_output_fails_that_probe():
    result = await probe(ScriptedLlm(replies=["sure thing!", TOOL_CALL, "done"]))
    assert not result.structured and result.tools
    assert "structured output failed" in result.detail


async def test_a_model_that_ignores_tools_fails_the_tool_probe():
    result = await probe(ScriptedLlm(replies=[STRUCTURED, "done"]))
    assert result.structured and not result.tools
    assert "didn't call the tool" in result.detail


async def test_an_unconfigured_role_is_reported_without_a_call():
    result = await probe_role(
        "smart",
        config=Config(),
        now=lambda: NOW,
        resolver=fake_resolver(unavailable="needs GEMINI_API_KEY"),
    )
    assert not result.ok and "GEMINI_API_KEY" in result.detail


def test_probe_results_round_trip_and_mark_the_roles(tmp_path):
    path = tmp_path / "probes.json"
    save_probe_results(
        path,
        [
            ProbeResult("fast", "fake/fake-model", True, False, "no tool call", NOW),
            ProbeResult("smart", "fake/fake-model", False, False, "bad JSON", NOW),
        ],
    )
    loaded = load_probe_results(path)
    assert loaded["fast"].at == NOW and not loaded["fast"].tools
    fast, smart = apply_probes(role_statuses(Config(), fake_resolver(ScriptedLlm())), loaded)
    assert fast.ok and fast.degraded and fast.detail == "no tool call"
    assert not smart.ok and smart.detail == "bad JSON"


def test_a_stale_or_damaged_probe_cache_is_ignored(tmp_path):
    path = tmp_path / "probes.json"
    save_probe_results(path, [ProbeResult("fast", "gemini/older-model", False, False, "x", NOW)])
    statuses = role_statuses(Config(), fake_resolver(ScriptedLlm()))
    assert apply_probes(statuses, load_probe_results(path)) == statuses  # probed another model
    path.write_text("{not json")
    assert load_probe_results(path) == {}
