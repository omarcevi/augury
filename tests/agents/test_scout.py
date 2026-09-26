import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from augury.agents import scout
from augury.agents.scout import ScoutDeps, ScoutReport, recover_interrupted_runs, run_scout
from augury.core.clock import local_day
from augury.core.config import Config, ExportConfig, ScoutConfig
from augury.core.db.digest_repo import DigestRepo
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.models import FetchResult, FetchState, RawItem, Source
from augury.sources.http import HttpClient, HttpError
from tests.helpers import (
    CountingHttp,
    NullHttp,
    ScriptedLlm,
    echo_triage,
    fake_resolver,
    summary_reply,
)
from tests.unit.test_extract_html import GENERIC

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


class FakeAdapter:
    recipe_type: ClassVar[str] = "fake"

    def __init__(self, titles: list[str] | None = None, error: Exception | None = None) -> None:
        self.titles, self.error = titles or [], error

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        if self.error:
            raise self.error
        items = [
            RawItem(source_id=source.id, url=f"https://x.com/{source.id}/{t}", title=t)
            for t in self.titles
        ]
        return FetchResult(items=items, state=FetchState(etag='"e1"'))


def deps(paths, adapters, now=NOW) -> ScoutDeps:
    config = Config(scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=0))
    return ScoutDeps(
        conn=open_db(paths, now=now),
        http=NullHttp(),
        config=config,
        lock_path=paths.scout_lock_file,
        adapters=adapters,
        now=lambda: now,
    )


OK_ADAPTERS = {
    "hf_papers": FakeAdapter(["p1", "p2"]),
    "hf_blog": FakeAdapter(["b1"]),
    "hf_community": FakeAdapter(["c1"]),
}


async def test_one_failing_source_makes_a_partial_run(paths):
    adapters = OK_ADAPTERS | {"hf_community": FakeAdapter(error=HttpError("u", "HTTP 503", 503))}
    d = deps(paths, adapters)
    report = await run_scout(d)
    assert report.status == "partial" and report.new_items == 3
    assert "HTTP 503" in (report.sources["hf-community"].error or "")
    sources = SourcesRepo(d.conn)
    assert sources.get("hf-community").health == "degraded"  # type: ignore[union-attr]
    assert sources.fetch_state("hf-papers").etag == '"e1"'
    assert RunsRepo(d.conn).last("scout").status == "partial"  # type: ignore[union-attr]


async def test_second_run_finds_nothing_new(paths):
    await run_scout(deps(paths, OK_ADAPTERS))
    report = await run_scout(deps(paths, OK_ADAPTERS, now=NOW + timedelta(hours=1)))
    assert report.status == "ok" and report.new_items == 0


async def test_every_source_failing_is_a_failed_run(paths):
    broken = {k: FakeAdapter(error=RuntimeError("boom")) for k in OK_ADAPTERS}
    assert (await run_scout(deps(paths, broken))).status == "failed"


async def test_only_one_source(paths):
    report = await run_scout(deps(paths, OK_ADAPTERS), only="hf-blog")
    assert list(report.sources) == ["hf-blog"]


async def test_unknown_only_source_is_an_error(paths):
    with pytest.raises(ValueError, match="nope"):
        await run_scout(deps(paths, OK_ADAPTERS), only="nope")


async def test_a_concurrent_scout_is_refused(paths):
    with ScoutLock(paths.scout_lock_file), pytest.raises(ScoutAlreadyRunning):
        await run_scout(deps(paths, OK_ADAPTERS))


def test_interrupted_runs_are_recovered_only_when_no_scout_is_live(paths):
    conn = open_db(paths, now=NOW)
    RunsRepo(conn).start("scout", now=NOW)
    with ScoutLock(paths.scout_lock_file):
        assert recover_interrupted_runs(conn, paths.scout_lock_file, now=NOW) == 0
    assert recover_interrupted_runs(conn, paths.scout_lock_file, now=NOW) == 1


class BlockingAdapter:
    recipe_type: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_cancelled_scout_is_recorded_as_interrupted(paths):
    blocking = BlockingAdapter()
    d = deps(paths, OK_ADAPTERS | {"hf_blog": blocking})
    task = asyncio.create_task(run_scout(d))
    await asyncio.wait_for(blocking.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert RunsRepo(d.conn).last("scout").status == "interrupted"  # type: ignore[union-attr]
    with ScoutLock(paths.scout_lock_file):  # released too
        pass


async def test_a_failing_on_stored_callback_never_fails_the_scout(paths, caplog):
    # A TUI refresh triggered by on_stored (e.g. a widget query on a torn-down screen)
    # must never take the whole scout down with it (P5.1).
    def boom(_report):
        raise RuntimeError("refresh blew up")

    d = deps(paths, OK_ADAPTERS)
    d.on_stored = boom
    report = await run_scout(d)
    assert report.status == "ok" and report.new_items == 4
    assert any(r.message == "on_stored callback failed" and r.exc_info for r in caplog.records)


async def test_the_scout_triages_new_items(paths):
    d = deps(paths, OK_ADAPTERS)
    d.resolver = fake_resolver(ScriptedLlm(replies=[echo_triage]))
    report = await run_scout(d)
    assert report.triage is not None and report.triage.triaged == 4
    usage = d.conn.execute("SELECT tokens_in, tokens_out FROM runs WHERE id = ?", (report.run_id,))
    assert tuple(usage.fetchone()) == (100, 20)


async def test_the_scout_without_a_key_still_succeeds(paths):
    d = deps(paths, OK_ADAPTERS)
    report = await run_scout(d)
    assert report.status == "ok"
    assert report.triage is not None and report.triage.degraded_kind == "not_configured"
    assert RunsRepo(d.conn).last("scout").error is None  # type: ignore[union-attr]  # not a fault


async def test_a_triage_failure_is_recorded_on_the_run(paths):
    d = deps(paths, OK_ADAPTERS)
    d.resolver = fake_resolver(ScriptedLlm(replies=[RuntimeError("503 Service Unavailable")]))
    report = await run_scout(d)
    assert report.status == "ok"  # the items are stored; only the ranking is degraded
    assert report.triage is not None and report.triage.degraded_kind == "provider"
    error = RunsRepo(d.conn).last("scout").error  # type: ignore[union-attr]
    assert error is not None and error.startswith("triage: provider error: RuntimeError: 503")


async def test_the_scout_builds_todays_digest_even_without_a_key(paths):
    d = deps(paths, OK_ADAPTERS)
    report = await run_scout(d)
    assert report.digest is not None and report.digest.items == 4
    assert report.digest.without_relevance == 4 and report.digest.mode == "cold"  # no key, no ★
    assert len(DigestRepo(d.conn).for_day(local_day(NOW))) == 4


async def test_the_ranked_list_is_announced_before_later_steps(paths):
    seen: list[ScoutReport] = []
    d = deps(paths, OK_ADAPTERS)
    d.on_stored = seen.append
    await run_scout(d)
    assert [r.digest is not None for r in seen] == [False, True]  # store, then rank


async def test_a_ranking_failure_is_recorded_and_the_scout_still_succeeds(paths, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bad weights")

    monkeypatch.setattr(scout, "build_digest", broken)
    d = deps(paths, OK_ADAPTERS)
    report = await run_scout(d)
    assert report.status == "ok" and report.new_items == 4 and report.digest is None
    assert RunsRepo(d.conn).last("scout").error == "digest: RuntimeError: bad weights"  # type: ignore[union-attr]


async def test_the_scout_reads_ahead_and_summarizes_the_top_items(paths):
    urls = ["hf-papers/p1", "hf-papers/p2", "hf-blog/b1", "hf-community/c1"]
    http = CountingHttp({f"https://x.com/{u}": GENERIC for u in urls})
    d = deps(paths, OK_ADAPTERS)
    d.http = http
    d.config = Config(scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=2))
    d.resolver = fake_resolver(ScriptedLlm(replies=[echo_triage, summary_reply(), summary_reply()]))
    report = await run_scout(d)
    assert report.prefetch is not None
    assert (report.prefetch.extracted, report.prefetch.summarized) == (2, 2)
    assert len(http.calls) == 2


async def test_without_a_key_the_scout_reads_nothing_ahead(paths):
    d = deps(paths, OK_ADAPTERS)  # NullHttp: any request would fail the test
    d.config = Config(scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=5))
    report = await run_scout(d)
    assert report.status == "ok" and report.prefetch is None


async def test_the_scout_exports_the_day_when_a_path_is_set(paths, tmp_path):
    d = deps(paths, OK_ADAPTERS)
    d.config = Config(
        scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=0),
        export=ExportConfig(path=str(tmp_path / "notes")),
    )
    report = await run_scout(d)
    assert report.exported is not None
    assert f"run_id: {report.run_id}" in Path(report.exported).read_text(encoding="utf-8")


async def test_an_export_failure_is_reported_not_fatal(paths, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    d = deps(paths, OK_ADAPTERS)
    d.config = Config(
        scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=0),
        export=ExportConfig(path=str(blocker)),
    )
    report = await run_scout(d)
    assert report.status == "ok" and report.export_error is not None
    last = RunsRepo(d.conn).last("scout")
    assert last is not None and "export:" in (last.error or "")


async def test_without_an_export_path_the_scout_writes_nothing(paths):
    report = await run_scout(deps(paths, OK_ADAPTERS))
    assert report.status == "ok" and report.exported is None and report.export_error is None


async def test_any_export_failure_is_reported_not_fatal(paths, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")  # not an OSError

    monkeypatch.setattr(scout, "export_daily", broken)
    d = deps(paths, OK_ADAPTERS)
    d.config = Config(
        scout=ScoutConfig(enrich_max_per_run=0, prefetch_top_n=0),
        export=ExportConfig(path=str(tmp_path / "notes")),
    )
    report = await run_scout(d)
    assert report.status == "ok" and report.new_items == 4 and report.digest is not None
    assert report.export_error == "OperationalError: database is locked"
    last = RunsRepo(d.conn).last("scout")
    assert last is not None and last.status == "ok"
    assert last.error == "export: OperationalError: database is locked"
