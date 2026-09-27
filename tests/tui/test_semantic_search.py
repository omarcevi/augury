from augury.core.config import Config
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.ingest import ingest_archive
from augury.tui.widgets.items_table import ItemsTable
from tests.rag.helpers import add_items
from tests.tui.conftest import NOW, QUIET, until


async def _seeded(app, embedder=None) -> list[str]:
    ids = add_items(
        app.conn,
        [
            ("Sparse attention kernels", "Fast long context."),
            ("Long inputs", "attention sparsity keeps long context cheap"),
            ("Tomato soup", "A recipe."),
        ],
        now=NOW,
    )
    if embedder is not None:
        meter = EmbedMeter(
            app.conn, Config(), RunsRepo(app.conn).start("scout", now=NOW), lambda: NOW
        )
        await ingest_archive(app.conn, embedder=embedder, meter=meter, now=lambda: NOW)
    return ids


def _listed(app) -> list[str]:
    return list(app.query_one(ItemsTable).rows_by_key)


async def test_enter_upgrades_the_typed_filter_to_semantic_search(make_app):
    embedder = HashEmbedder(64)
    app = make_app(embedder=embedder)
    ids = await _seeded(app, embedder)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", *"sparse attention")
        await until(pilot, lambda: _listed(app) == [ids[0]])  # typed: keywords, AND
        await pilot.press("enter")
        await until(pilot, lambda: app.semantic is not None)
        assert _listed(app)[:2] == [ids[0], ids[1]]  # by meaning, best first
        assert "· semantic" in str(app.query_one("#items-pane").border_title)
        assert embedder.calls[-1] == ("query", 1)
        run = RunsRepo(app.conn).last("embed")
        assert run is not None and run.status == "ok"
        await pilot.press("slash", "s")  # typing again: back to the live keyword filter
        await until(pilot, lambda: app.semantic is None)
        assert "semantic" not in str(app.query_one("#items-pane").border_title)


async def test_without_an_embedder_enter_keeps_the_keyword_results(make_app):
    app = make_app(config=QUIET)  # no key in tests: no embedder
    ids = await _seeded(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", *"sparse", "enter")
        await until(
            pilot, lambda: any("Keyword results only" in n.message for n in app._notifications)
        )
        assert _listed(app) == [ids[0]] and app.semantic is None
        assert "GEMINI_API_KEY" in next(
            n.message for n in app._notifications if "Keyword results only" in n.message
        )
        assert RunsRepo(app.conn).last("embed") is None  # nothing to spend: no run


async def test_semantic_results_keep_the_views_other_filters(make_app):
    from augury.core.db.state_repo import StateRepo

    embedder = HashEmbedder(64)
    app = make_app(embedder=embedder)
    ids = await _seeded(app, embedder)
    StateRepo(app.conn).toggle(ids[1], "hidden", now=NOW)  # Unread leaves hidden items out
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", *"attention", "enter")
        await until(pilot, lambda: app.semantic is not None)
        assert ids[1] not in _listed(app) and _listed(app)[0] == ids[0]
