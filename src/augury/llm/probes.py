"""Doctor's capability probes (spec §10): for each role, one structured-output call and one
tool call, through the same make_agent/node path the agents use. Results are saved so the
health bar can show them without calling anything."""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from google.adk.agents import Context
from pydantic import BaseModel

from augury.agents.llm_step import describe, make_agent, node_caller, run_in_node
from augury.core.clock import from_iso, to_iso
from augury.core.config import Config
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import ROLES, ModelUnavailable, Resolver, Role, RoleStatus, model_spec

PROBE_WORD = "augury"


class ProbeAnswer(BaseModel):
    ok: bool
    word: str


@dataclass(frozen=True)
class ProbeResult:
    role: Role
    spec: str
    structured: bool
    tools: bool
    detail: str
    at: datetime

    @property
    def ok(self) -> bool:
        return self.structured and self.tools


async def probe_role(
    role: Role, *, config: Config, resolver: Resolver, now: Callable[[], datetime]
) -> ProbeResult:
    spec = model_spec(config, role)
    try:
        model = resolver(role, None)
    except ModelUnavailable as exc:
        return ProbeResult(role, spec, False, False, str(exc), now())
    template = load_prompt("probe")
    words: list[str] = []

    def record_word(word: str) -> dict[str, str]:
        """Record the word you were asked to send."""
        words.append(word)
        return {"recorded": word}

    # No ledger: the doctor checks the budget before probing, and a probe costs ~100 tokens.
    structured_agent = make_agent("probe_structured", template, model, output_schema=ProbeAnswer)
    tools_agent = make_agent("probe_tools", template, model, tools=[record_word])

    async def body(ctx: Context) -> tuple[bool, bool, str]:
        problems: list[str] = []
        structured = tools = False
        try:
            reply = await node_caller(ctx, structured_agent)(
                f"Reply with ok set to true and word set to {PROBE_WORD!r}."
            )
            answer = ProbeAnswer.model_validate(reply)
            structured = answer.ok and answer.word.strip().lower() == PROBE_WORD
            if not structured:
                problems.append("structured output came back wrong")
        except Exception as exc:
            problems.append(f"structured output failed: {describe(exc, 160)}")
        try:
            await node_caller(ctx, tools_agent)(
                f"Call record_word with the word {PROBE_WORD!r}, then reply 'done'."
            )
            tools = PROBE_WORD in (w.strip().lower() for w in words)
            if not tools:
                problems.append("the model didn't call the tool")
        except Exception as exc:
            problems.append(f"tool call failed: {describe(exc, 160)}")
        return structured, tools, "; ".join(problems)

    structured, tools, detail = await run_in_node("probe", body)
    return ProbeResult(role, model.spec, structured, tools, detail, now())


def save_probe_results(path: Path, results: Iterable[ProbeResult]) -> None:
    data = {
        r.role: {
            "spec": r.spec,
            "structured": r.structured,
            "tools": r.tools,
            "detail": r.detail,
            "at": to_iso(r.at),
        }
        for r in results
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _result(role: Role, row: Any) -> ProbeResult:
    return ProbeResult(
        role,
        str(row["spec"]),
        bool(row["structured"]),
        bool(row["tools"]),
        str(row["detail"]),
        from_iso(row["at"]),
    )


def load_probe_results(path: Path) -> dict[str, ProbeResult]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {role: _result(role, data[role]) for role in ROLES if role in data}
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        return {}  # missing or damaged: as if doctor never ran


def apply_probes(
    statuses: Iterable[RoleStatus], probes: Mapping[str, ProbeResult]
) -> tuple[RoleStatus, ...]:
    """A configured role whose last probe (of the same model) failed shows ✗; one that only
    failed the tool call shows ⚠ (fine for triage and TL;DRs, not for M3's discovery)."""
    marked: list[RoleStatus] = []
    for status in statuses:
        probe = probes.get(status.role)
        if status.ok and probe is not None and probe.spec == status.spec:
            if not probe.structured:
                status = replace(status, ok=False, detail=probe.detail)
            elif not probe.tools:
                status = replace(status, degraded=True, detail=probe.detail)
        marked.append(status)
    return tuple(marked)
