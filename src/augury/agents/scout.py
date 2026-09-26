import logging
import random
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from google.adk.agents import Context
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel

from augury.agents.enrich import enrich_new_articles
from augury.agents.llm_step import node_caller, run_workflow
from augury.agents.normalize import store_items
from augury.agents.rank import DigestStats, build_digest
from augury.agents.triage import TriageStats, triage_new_items
from augury.core.clock import local_day, utcnow
from augury.core.config import Config, Interests
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.models import FetchResult, Source
from augury.llm.resolver import ModelUnavailable, Resolver, default_resolver
from augury.sources.base import Adapter
from augury.sources.http import HttpClient
from augury.sources.registry import ADAPTERS

_log = logging.getLogger(__name__)


class SourceStats(BaseModel):
    fetched: int = 0
    new: int = 0
    skipped: int = 0
    not_modified: bool = False
    error: str | None = None


class ScoutReport(BaseModel):
    run_id: str
    status: Literal["ok", "partial", "failed"]
    sources: dict[str, SourceStats]
    new_items: int
    enriched: int = 0
    enrich_error: str | None = None
    triage: TriageStats | None = None  # M2
    digest: DigestStats | None = None  # M2
    digest_error: str | None = None

    def problems(self) -> list[str]:
        """Everything that went wrong, for runs.error (spec §11: nothing fails silently)."""
        found = [f"{sid}: {s.error}" for sid, s in self.sources.items() if s.error]
        if self.enrich_error:
            found.append(f"enrich: {self.enrich_error}")
        if self.triage is not None and self.triage.degraded_kind in ("budget", "provider"):
            found.append(f"triage: {self.triage.degraded}")
        if self.digest_error:
            found.append(f"digest: {self.digest_error}")
        return found


@dataclass
class ScoutDeps:
    conn: sqlite3.Connection
    http: HttpClient
    config: Config
    lock_path: Path
    adapters: Mapping[str, Adapter] = field(default_factory=lambda: ADAPTERS)
    now: Callable[[], datetime] = utcnow
    # Called on the event loop as soon as items are stored, before the slower enrichment.
    on_stored: Callable[[ScoutReport], None] | None = None
    interests: Interests = field(default_factory=Interests)  # M2
    resolver: Resolver | None = None  # M2; None resolves from config and the environment
    rng: random.Random = field(default_factory=random.Random)  # M2; tests pin the shuffle


@dataclass
class _Outcome:
    result: FetchResult | None = None
    error: str | None = None


def build_scout_workflow(
    deps: ScoutDeps, sources: Sequence[Source], run_id: str, sink: list[ScoutReport]
) -> Workflow:
    # Fetched items stay in this closure; node outputs carry only ids and the small report.
    by_id = {s.id: s for s in sources}
    outcomes: dict[str, _Outcome] = {}
    sources_repo = SourcesRepo(deps.conn)
    resolver = deps.resolver or default_resolver(deps.config)

    def announce(report: ScoutReport) -> None:
        """Tell the caller (the TUI) there is something new to show. Store calls it, and so
        does any later step that changes the list (ranking), so it never waits for the rest."""
        if deps.on_stored is None:
            return
        try:
            deps.on_stored(report)
        except Exception:  # a TUI refresh failing (e.g. a torn-down widget) must
            # never fail the scout itself (P5.1) -- the items are already stored.
            _log.exception("on_stored callback failed")

    def plan_sources(node_input: types.Content) -> list[str]:
        return list(by_id)

    @node(parallel_worker=True)
    async def fetch_source(node_input: str) -> str:
        source = by_id[node_input]
        try:
            adapter = deps.adapters[source.recipe.type]
            result = await adapter.fetch(source, sources_repo.fetch_state(source.id), deps.http)
        except Exception as exc:  # isolation: one broken source never fails the run
            outcomes[source.id] = _Outcome(error=f"{type(exc).__name__}: {exc}")
        else:
            outcomes[source.id] = _Outcome(result=result)
        return source.id

    def store(node_input: list[str]) -> ScoutReport:
        now = deps.now()
        stats: dict[str, SourceStats] = {}
        for source_id in node_input:
            outcome = outcomes[source_id]
            if outcome.result is None:
                error = outcome.error or "no result"
                sources_repo.record_failure(source_id, error, now=now)
                stats[source_id] = SourceStats(error=error)
                continue
            stored = store_items(deps.conn, outcome.result.items, now=now)
            sources_repo.record_success(source_id, outcome.result.state, now=now)
            stats[source_id] = SourceStats(
                fetched=stored.seen,
                new=stored.new,
                skipped=outcome.result.skipped + stored.skipped,
                not_modified=outcome.result.not_modified,
            )
        failed = sum(1 for s in stats.values() if s.error)
        status = "ok" if failed == 0 else "failed" if failed == len(stats) else "partial"
        report = ScoutReport(
            run_id=run_id,
            status=status,
            sources=stats,
            new_items=sum(s.new for s in stats.values()),
        )
        sink.append(report)
        announce(report)
        return report

    async def enrich(node_input: ScoutReport) -> ScoutReport:
        try:
            result = await enrich_new_articles(
                deps.conn, deps.http, now=deps.now(), limit=deps.config.scout.enrich_max_per_run
            )
        except Exception as exc:  # best effort: the items are already stored
            update = {"enrich_error": f"{type(exc).__name__}: {exc}"}
        else:
            update = {"enriched": result.enriched, "enrich_error": result.error}
        report = node_input.model_copy(update=update)
        sink.append(report)
        return report

    # M2: the triage LlmAgent runs as a child node of this one (ctx.run_node needs
    # rerun_on_resume=True). Batching, repair and bisection are code, in agents/triage.py.
    @node(rerun_on_resume=True)
    async def triage(ctx: Context, node_input: ScoutReport) -> ScoutReport:
        try:
            stats = await triage_new_items(
                deps.conn,
                config=deps.config,
                interests=deps.interests,
                resolver=resolver,
                run_id=run_id,
                now=deps.now,
                rng=deps.rng,
                make_call=lambda agent: node_caller(ctx, agent),
            )
        except Exception as exc:  # best effort: the digest is then ranked without relevance
            stats = TriageStats(
                degraded=f"triage crashed: {type(exc).__name__}: {exc}", degraded_kind="provider"
            )
        report = node_input.model_copy(update={"triage": stats})
        sink.append(report)
        return report

    def rank(node_input: ScoutReport) -> ScoutReport:
        now = deps.now()
        try:  # ranking is code: it runs with or without a key (spec §5.3)
            try:
                resolver("fast", "triage")  # builds the model object only; nothing is sent
                ai = True
            except ModelUnavailable:  # no key: your likes + recency (user decision 2026-09-26)
                ai = False
            stats = build_digest(
                deps.conn, local_day(now), now=now, config=deps.config.ranking, ai=ai
            )
        except Exception as exc:  # the items are stored; a ranking bug must not lose the scout
            report = node_input.model_copy(update={"digest_error": f"{type(exc).__name__}: {exc}"})
        else:
            report = node_input.model_copy(update={"digest": stats})
        sink.append(report)
        announce(report)  # the TUI shows the ranked list now, not after the later steps
        return report

    return Workflow(
        name="scout",
        edges=[
            ("START", plan_sources),
            (plan_sources, fetch_source),
            (fetch_source, store),
            (store, enrich),
            (enrich, triage),
            (triage, rank),
        ],
    )


async def run_scout(deps: ScoutDeps, *, only: str | None = None) -> ScoutReport:
    with ScoutLock(deps.lock_path):
        runs = RunsRepo(deps.conn)
        sources = [
            r.source
            for r in SourcesRepo(deps.conn).list_all(enabled_only=True)
            if only is None or r.source.id == only
        ]
        if only is not None and not sources:
            raise ValueError(f"no enabled source with id {only!r}")
        run_id = runs.start("scout", now=deps.now())
        sink: list[ScoutReport] = []
        try:
            if sources:
                await run_workflow(build_scout_workflow(deps, sources, run_id, sink), "scout")
            else:
                sink.append(ScoutReport(run_id=run_id, status="ok", sources={}, new_items=0))
        except Exception as exc:
            runs.finish(run_id, "failed", now=deps.now(), error=f"{type(exc).__name__}: {exc}")
            raise
        except BaseException:  # cancelled (e.g. the TUI quit) or Ctrl-C: never left "running"
            runs.finish(run_id, "interrupted", now=deps.now())
            raise
        if not sink:
            runs.finish(run_id, "failed", now=deps.now(), error="the workflow produced no report")
            raise RuntimeError("scout workflow finished without a report")
        report = sink[-1]
        runs.finish(
            run_id,
            report.status,
            now=deps.now(),
            error="; ".join(report.problems()) or None,
            stats=report.model_dump(),
        )
        return report


def recover_interrupted_runs(conn: sqlite3.Connection, lock_path: Path, *, now: datetime) -> int:
    """Mark stale 'running' scouts as interrupted, but only if no live scout holds the lock."""
    lock = ScoutLock(lock_path)
    try:
        lock.acquire()
    except ScoutAlreadyRunning:
        return 0
    try:
        return RunsRepo(conn).mark_running_as_interrupted("scout", now=now)
    finally:
        lock.release()
