import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel

from augury.agents.enrich import enrich_new_articles
from augury.agents.normalize import store_items
from augury.core.clock import utcnow
from augury.core.config import Config
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.models import FetchResult, Source
from augury.sources.base import Adapter
from augury.sources.http import HttpClient
from augury.sources.registry import ADAPTERS

APP_NAME = "augury"


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


@dataclass
class ScoutDeps:
    conn: sqlite3.Connection
    http: HttpClient
    config: Config
    lock_path: Path
    adapters: Mapping[str, Adapter] = field(default_factory=lambda: ADAPTERS)
    now: Callable[[], datetime] = utcnow


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
        return report

    async def enrich(node_input: ScoutReport) -> ScoutReport:
        try:
            count = await enrich_new_articles(
                deps.conn, deps.http, now=deps.now(), limit=deps.config.scout.enrich_max_per_run
            )
        except Exception as exc:  # best effort: the items are already stored
            report = node_input.model_copy(update={"enrich_error": f"{type(exc).__name__}: {exc}"})
        else:
            report = node_input.model_copy(update={"enriched": count})
        sink.append(report)
        return report

    return Workflow(
        name="scout",
        edges=[
            ("START", plan_sources),
            (plan_sources, fetch_source),
            (fetch_source, store),
            (store, enrich),
        ],
    )


async def _run_workflow(workflow: Workflow) -> None:
    runner = InMemoryRunner(app=App(name=APP_NAME, root_agent=workflow))
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id="local")
    message = types.Content(role="user", parts=[types.Part.from_text(text="scout")])
    async for _ in runner.run_async(user_id="local", session_id=session.id, new_message=message):
        pass


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
                await _run_workflow(build_scout_workflow(deps, sources, run_id, sink))
            else:
                sink.append(ScoutReport(run_id=run_id, status="ok", sources={}, new_items=0))
        except Exception as exc:
            runs.finish(run_id, "failed", now=deps.now(), error=f"{type(exc).__name__}: {exc}")
            raise
        if not sink:
            runs.finish(run_id, "failed", now=deps.now(), error="the workflow produced no report")
            raise RuntimeError("scout workflow finished without a report")
        report = sink[-1]
        errors = [f"{sid}: {s.error}" for sid, s in report.sources.items() if s.error]
        runs.finish(
            run_id,
            report.status,
            now=deps.now(),
            error="; ".join(errors) or None,
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
