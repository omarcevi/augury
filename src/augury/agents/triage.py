"""Triage (spec §5.3). Today's untriaged items, shuffled and cut into batches of at most 50, go
to the fast model as fenced data. A reply must cover each idx exactly once. A bad reply gets one
repair attempt that quotes the error; if that fails too, the batch is bisected down to single
items, and a single item that still fails is saved as triage_failed."""

import json
import random
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from augury.agents.llm_step import (
    Call,
    SchemaFailure,
    describe,
    is_budget_stop,
    is_schema_failure,
    make_agent,
    unwrap,
)
from augury.core.clock import local_day, local_day_bounds
from augury.core.config import Config, Interests
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import HIDING_FLAGS, TRIAGE_FAILED, Item, Signals, TriageResult
from augury.core.text import strip_control_chars
from augury.llm.budget import UsageLedger, budget_problem, today_spend
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import ModelUnavailable, Resolver
from augury.llm.safe_prompt import prompt

BATCH_SIZE = 50
SUMMARY_CHARS = 600
WHY_READ_CHARS = 140
MAX_TAGS = 3
TAG_CHARS = 32
NOT_CONFIGURED = "AI not configured"
DegradedKind = Literal["not_configured", "budget", "provider"]


class TriageEntry(BaseModel):
    idx: int
    relevance: int = Field(ge=0, le=10)
    why_read: str
    tags: list[str] = Field(default_factory=list)
    flags: list[Literal["promo", "thin", "off_topic"]] = Field(default_factory=list)


class TriageBatch(BaseModel):
    """One batch's reply. Also the LlmAgent's output_schema, so providers enforce the shape."""

    items: list[TriageEntry]


class TriageStats(BaseModel):
    attempted: int = 0
    triaged: int = 0
    failed: int = 0
    hidden: int = 0
    calls: int = 0
    degraded: str | None = None  # why some or all items are ranked without relevance
    degraded_kind: DegradedKind | None = None


@dataclass(frozen=True)
class BatchItem:
    idx: int
    item_id: str
    payload: dict[str, object]


class TriageStopped(Exception):
    def __init__(self, kind: DegradedKind, reason: str) -> None:
        super().__init__(reason)
        self.kind: DegradedKind = kind
        self.reason = reason


def _clip(text: str, limit: int) -> str:
    text = " ".join(strip_control_chars(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tags(tags: Iterable[str]) -> list[str]:
    kept: list[str] = []
    for raw in tags:
        tag = _clip(raw, TAG_CHARS).lower()
        if tag and tag not in kept:
            kept.append(tag)
    return kept[:MAX_TAGS]


def accept(batch: Sequence[BatchItem], output: object) -> list[TriageResult]:
    """Check one reply against its batch: every idx exactly once (spec §5.3)."""
    if output is None:
        raise SchemaFailure("the reply was empty")
    reply = TriageBatch.model_validate(output)
    by_idx = {b.idx: b for b in batch}
    seen = Counter(entry.idx for entry in reply.items)
    problems: list[str] = []
    if repeated := sorted(i for i, n in seen.items() if n > 1):
        problems.append(f"idx repeated: {repeated}")
    if missing := sorted(set(by_idx) - set(seen)):
        problems.append(f"idx missing: {missing}")
    if unknown := sorted(set(seen) - set(by_idx)):
        problems.append(f"idx not in this batch: {unknown}")
    if problems:
        raise SchemaFailure("; ".join(problems))
    return [
        TriageResult(
            item_id=by_idx[entry.idx].item_id,
            relevance=entry.relevance,
            why_read=_clip(entry.why_read, WHY_READ_CHARS),
            tags=_tags(entry.tags),
            flags=sorted(set(entry.flags)),
        )
        for entry in reply.items
    ]


def _payload(
    idx: int, item: Item, source_names: Mapping[str, str], signals: Signals | None
) -> dict[str, object]:
    return {
        "idx": idx,
        "source": source_names.get(item.source_id, item.source_id),
        "kind": item.kind,
        "title": item.title,
        "summary": item.summary[:SUMMARY_CHARS],
        "signals": signals.model_dump(exclude_none=True) if signals else {},
    }


def make_batches(
    items: Sequence[Item],
    source_names: Mapping[str, str],
    signals: Mapping[str, Signals],
    rng: random.Random,
) -> list[list[BatchItem]]:
    order = list(items)
    rng.shuffle(order)  # spec §5.3: a new order every run, against position bias
    batches: list[list[BatchItem]] = []
    for start in range(0, len(order), BATCH_SIZE):
        chunk = order[start : start + BATCH_SIZE]
        batches.append(
            [
                BatchItem(i, item.id, _payload(i, item, source_names, signals.get(item.id)))
                for i, item in enumerate(chunk)
            ]
        )
    return batches


def _idx_list(batch: Sequence[BatchItem]) -> str:
    return ", ".join(str(b.idx) for b in batch)


def batch_prompt(batch: Sequence[BatchItem], interests: Interests) -> str:
    profile = json.dumps(
        {"about": interests.about, "topics": interests.topics, "avoid": interests.avoid},
        ensure_ascii=False,
    )
    items = json.dumps([b.payload for b in batch], ensure_ascii=False)
    idx = _idx_list(batch)
    # The profile is the user's own file (trusted); the items are fetched text (data).
    return prompt(
        t"Reader profile:\n{profile:trusted}\n\nItems to triage:\n{items}\n\n"
        t"Reply with exactly one entry for each idx: {idx:trusted}."
    )


def repair_prompt(original: str, error: str, batch: Sequence[BatchItem]) -> str:
    idx = _idx_list(batch)
    # The error quotes the rejected reply, which can echo fetched text, so it is data too.
    return prompt(
        t"{original:trusted}\n\nYour previous reply was rejected because:\n{error}\n"
        t"Reply again with valid JSON and exactly one entry for each idx: {idx:trusted}."
    )


class Triager:
    def __init__(self, call: Call, interests: Interests) -> None:
        self.call, self.interests = call, interests
        self.results: dict[str, TriageResult] = {}
        self.calls = 0

    async def _ask(self, text: str, batch: Sequence[BatchItem]) -> list[TriageResult] | str:
        """One model call: the results, or the reason the reply was rejected."""
        self.calls += 1
        try:
            return accept(batch, await self.call(text))
        except Exception as exc:
            if is_schema_failure(exc):
                return describe(exc)
            if is_budget_stop(exc):
                raise TriageStopped("budget", str(unwrap(exc))) from exc
            raise TriageStopped("provider", f"provider error: {describe(exc, 300)}") from exc

    async def run_batch(self, batch: list[BatchItem]) -> None:
        text = batch_prompt(batch, self.interests)
        outcome = await self._ask(text, batch)
        if isinstance(outcome, str):  # one repair attempt, quoting the error
            outcome = await self._ask(repair_prompt(text, outcome, batch), batch)
        if not isinstance(outcome, str):
            self.results.update((r.item_id, r) for r in outcome)
        elif len(batch) == 1:
            self.results[batch[0].item_id] = TriageResult.failed(batch[0].item_id)
        else:  # bisect: each half gets its own attempt and repair, down to single items
            middle = len(batch) // 2
            await self.run_batch(batch[:middle])
            await self.run_batch(batch[middle:])


async def triage_new_items(
    conn: sqlite3.Connection,
    *,
    config: Config,
    interests: Interests,
    resolver: Resolver,
    run_id: str,
    now: Callable[[], datetime],
    rng: random.Random,
    make_call: Callable[[LlmAgent], Call],
) -> TriageStats:
    start, end = local_day_bounds(local_day(now()))
    items = TriageRepo(conn).untriaged(start, end)
    if not items:
        return TriageStats()
    try:
        model = resolver("fast", "triage")
    except ModelUnavailable:
        return TriageStats(
            attempted=len(items), degraded=NOT_CONFIGURED, degraded_kind="not_configured"
        )
    if (problem := budget_problem(today_spend(conn, now()), config.budget)) is not None:
        return TriageStats(attempted=len(items), degraded=problem, degraded_kind="budget")
    template = load_prompt("triage")
    ledger = UsageLedger(conn, run_id, config, model, now)
    agent = make_agent("triage_llm", template, model, ledger=ledger, output_schema=TriageBatch)
    triager = Triager(make_call(agent), interests)
    names = {r.source.id: r.source.name for r in SourcesRepo(conn).list_all()}
    signals = ItemsRepo(conn).latest_signals([item.id for item in items])
    stopped: TriageStopped | None = None
    try:
        for batch in make_batches(items, names, signals, rng):
            await triager.run_batch(batch)
    except TriageStopped as stop:  # keep what's done; the rest waits for the next scout
        stopped = stop
    results = list(triager.results.values())
    TriageRepo(conn).save_all(
        results, run_id=run_id, model=model.spec, prompt_version=template.version
    )
    return TriageStats(
        attempted=len(items),
        triaged=sum(1 for r in results if r.relevance is not None),
        failed=sum(1 for r in results if TRIAGE_FAILED in r.flags),
        hidden=sum(1 for r in results if HIDING_FLAGS & set(r.flags)),
        calls=triager.calls,
        degraded=stopped.reason if stopped else None,
        degraded_kind=stopped.kind if stopped else None,
    )
