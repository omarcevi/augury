import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from augury.agents.scout import ScoutDeps, recover_interrupted_runs, run_scout
from augury.core.config import Config, ScoutConfig
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.lock import ScoutAlreadyRunning, ScoutLock
from augury.core.models import FetchResult, FetchState, RawItem, Source
from augury.sources.http import HttpClient, HttpError
from tests.helpers import NullHttp

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
    config = Config(scout=ScoutConfig(enrich_max_per_run=0))
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
