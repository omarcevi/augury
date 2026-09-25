import asyncio
from datetime import timedelta

from augury.core.config import Config, ScoutConfig
from augury.core.db.runs_repo import RunsRepo
from augury.core.lock import ScoutLock
from augury.tui.widgets.health_bar import HealthBar
from tests.helpers import CountingHttp, SlowHttp
from tests.tui.conftest import NOW

AUTO = Config(scout=ScoutConfig(auto_after_hours=12, enrich_max_per_run=0))


def _finished_scout(app, hours_ago: float) -> None:
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW - timedelta(hours=hours_ago))
    runs.finish(run_id, "ok", now=NOW - timedelta(hours=hours_ago))


def _scout_count(app) -> int:
    return app.conn.execute("SELECT count(*) FROM runs WHERE kind = 'scout'").fetchone()[0]


async def test_stale_launch_scouts_in_the_background(make_app):
    app = make_app(config=AUTO, http=CountingHttp())
    _finished_scout(app, hours_ago=13)
    async with app.run_test(size=(140, 40)):
        await app.workers.wait_for_complete()
        assert _scout_count(app) == 2


async def test_fresh_launch_does_not_scout(make_app):
    app = make_app(config=AUTO, http=CountingHttp())
    _finished_scout(app, hours_ago=1)
    async with app.run_test(size=(140, 40)):
        await app.workers.wait_for_complete()
        assert _scout_count(app) == 1


async def test_r_scouts_on_demand(make_app):
    app = make_app(http=CountingHttp())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("r")
        await app.workers.wait_for_complete()
        assert _scout_count(app) == 1


async def test_offline_start_shows_failed_scout_without_crashing(make_app):
    http = CountingHttp()  # every request 404s, as if the network were down
    app = make_app(config=AUTO, http=http)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert http.calls  # it did try
        assert "failed" in app.query_one(HealthBar).render().plain
        assert app.is_running


async def test_quit_mid_scout_does_not_crash(make_app):
    # Config() (not AUTO/QUIET): the default auto_after_hours=12 with no prior run still
    # auto-scouts on mount, which is what the reviewer's repro relied on.
    http = SlowHttp()
    app = make_app(config=Config(), http=http)
    async with app.run_test(size=(140, 40)):
        await asyncio.wait_for(http.started.wait(), timeout=5)  # scout is now "on the network"
        app.exit()
        # No pilot.pause() here: leaving the `async with` right after exit() is what races
        # the worker's cancellation against run_test()'s DOM teardown (a `pause()` first
        # lets the loop settle and hides the race). `run_test()`'s __aexit__ re-raises any
        # fatal worker exception (e.g. WorkerFailed from a NoMatches against a torn-down
        # DOM); reaching the line below means it didn't.
    assert app.return_code in (0, None)
    with ScoutLock(app.paths.scout_lock_file):  # raises ScoutAlreadyRunning if the lock leaked
        pass
