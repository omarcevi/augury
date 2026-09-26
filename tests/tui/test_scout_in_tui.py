import asyncio
from datetime import timedelta

from textual.widgets import Static

from augury.agents import scout
from augury.agents.normalize import store_items
from augury.core.config import Config, ScoutConfig
from augury.core.db.runs_repo import RunsRepo
from augury.core.lock import ScoutLock
from augury.core.models import RawItem
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.items_table import ItemsTable
from tests.helpers import CountingHttp, HfHttp, SlowHttp
from tests.tui.conftest import NOW, until

AUTO = Config(scout=ScoutConfig(auto_after_hours=12, enrich_max_per_run=0))
MANUAL = Config(scout=ScoutConfig(auto_after_hours=0, enrich_max_per_run=0))


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


async def test_a_new_local_day_scouts_even_within_the_hours(make_app):
    app = make_app(config=AUTO, http=CountingHttp())
    _finished_scout(app, hours_ago=11)  # 22:00 yesterday; NOW is 09:00 (TZ=UTC in tests)
    async with app.run_test(size=(140, 40)):
        await app.workers.wait_for_complete()
        assert _scout_count(app) == 2


async def test_items_appear_as_soon_as_they_are_stored(make_app):
    http = HfHttp()
    http.page_gate.clear()  # enrichment (article pages) waits; the API fetches don't
    app = make_app(config=Config(), http=http)  # never scouted: auto-scouts on launch
    async with app.run_test(size=(140, 40)) as pilot:
        await asyncio.wait_for(http.page_started.wait(), timeout=5)  # stored, now enriching
        table = app.query_one(ItemsTable)
        await until(pilot, lambda: table.row_count > 0)
        assert any(w.group == "scout" and not w.is_finished for w in app.workers)
        assert "Scouting…" in app.query_one(HealthBar).render().plain
        assert "· 15 new  │" in app.query_one(HealthBar).render().plain  # a first launch
        http.page_gate.set()
        await app.workers.wait_for_complete()


def _empty_text(app) -> str:
    return str(app.query_one("#empty", Static).render())


async def test_empty_view_says_a_scout_is_running(make_app):
    http = HfHttp()
    http.api_gate.clear()
    app = make_app(config=Config(), http=http)
    async with app.run_test(size=(140, 40)) as pilot:
        await asyncio.wait_for(http.api_started.wait(), timeout=5)
        await pilot.pause()
        assert "Scouting… items will appear here" in _empty_text(app)
        http.api_gate.set()
        await app.workers.wait_for_complete()


async def test_empty_view_without_items_offers_a_scout(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)):
        assert _empty_text(app) == "No items yet — press r to scout"


async def test_empty_view_with_everything_filtered_out_says_how_to_widen(make_app):
    app = make_app()
    store_items(app.conn, [RawItem(source_id="hf-blog", url="https://x/a", title="A")], now=NOW)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("x")  # hide the only item
        assert app.query_one("#empty").display is True
        assert _empty_text(app) == "No items match these filters — press D or v to widen"


async def test_r_scouts_on_demand(make_app):
    app = make_app(http=CountingHttp())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("r")
        await app.workers.wait_for_complete()
        assert _scout_count(app) == 1


async def test_a_failed_enrichment_is_announced(make_app, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("enrich [b]exploded[/b]")

    monkeypatch.setattr(scout, "enrich_new_articles", boom)
    app = make_app(http=HfHttp())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        notes = [n for n in app._notifications if "Enrichment failed" in n.message]
        assert notes and notes[0].severity == "warning" and not notes[0].markup
        assert "RuntimeError: enrich [b]exploded[/b]" in notes[0].message
        assert "enrich ⚠" in app.query_one(HealthBar).render().plain


async def test_r_during_a_scout_keeps_the_running_one(make_app):
    http = HfHttp()
    http.api_gate.clear()
    app = make_app(config=MANUAL, http=http)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("r")
        await asyncio.wait_for(http.api_started.wait(), timeout=5)
        await pilot.press("r")
        await pilot.pause()
        assert [n.message for n in app._notifications] == ["Already scouting…"]
        http.api_gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        statuses = [r[0] for r in app.conn.execute("SELECT status FROM runs WHERE kind = 'scout'")]
        assert statuses == ["ok"]
        assert app.query_one(ItemsTable).row_count > 0
        assert "Scout: 09:00" in app.query_one(HealthBar).render().plain


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
