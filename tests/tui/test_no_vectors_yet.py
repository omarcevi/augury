"""An embedder is configured and sqlite-vec loads, but no vector is stored yet, so chunks_vec
doesn't exist: an M3 database on its first M4 launch, the first scout still running, or no
embedding ever succeeded (budget, provider). The reader, Enter in `/` and Ask stay usable,
keyword-only, and nothing is spent on a query embedding (spec §11)."""

import pytest
from textual.widgets import Static

from augury.core.db.chunks_repo import ChunksRepo, vec_dimensions
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import HashEmbedder
from augury.rag.ingest import ingest_archive
from augury.tui.widgets.ask_drawer import AskDrawer
from augury.tui.widgets.items_table import ItemsTable
from tests.helpers import CountingHttp, ScriptedLlm, fake_resolver
from tests.rag.helpers import BrokenEmbedder, add_items
from tests.tui.conftest import NOW, until
from tests.unit.test_extract_html import GENERIC

URL = "https://example.com/hf-blog/0-Sparse-attention-kernels"


async def _keyword_only(app) -> list[str]:
    ids = add_items(app.conn, [("Sparse attention kernels", "x"), ("Tomato soup", "y")], now=NOW)
    await ingest_archive(app.conn, embedder=None, meter=None, now=lambda: NOW)
    assert vec_dimensions(app.conn) is None
    return ids


def _toast(app, start: str) -> str | None:
    return next((n.message for n in app._notifications if n.message.startswith(start)), None)


def _indexed(app) -> bool:
    run = RunsRepo(app.conn).last("embed")
    return run is not None and run.status != "running"


@pytest.mark.parametrize("broken", [False, True], ids=["first-open", "embedding-fails"])
async def test_opening_an_item_before_any_vector_exists_never_crashes(make_app, broken):
    embedder = BrokenEmbedder(64) if broken else HashEmbedder(64)
    app = make_app(embedder=embedder, http=CountingHttp({URL: GENERIC}))
    ids = await _keyword_only(app)
    async with app.run_test(size=(160, 40)) as pilot:
        app.query_one(ItemsTable).select_key(ids[0])
        await pilot.press("enter")
        await until(pilot, lambda: ChunksRepo(app.conn).for_item(ids[0], "content") != [])
        await until(pilot, lambda: _indexed(app))  # the full text: an `embed` run, finished
        assert app.is_running and app.reading_id == ids[0]
        assert not app.query_one("#reader-related", Static).display  # no panel, no reason


async def test_enter_before_any_vector_exists_keeps_the_keywords_and_spends_nothing(make_app):
    embedder = HashEmbedder(64)
    app = make_app(embedder=embedder)
    ids = await _keyword_only(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", *"sparse", "enter")
        await until(pilot, lambda: _toast(app, "Keyword results only") is not None)
        assert "no passage has a vector yet" in (_toast(app, "Keyword results only") or "")
        assert list(app.query_one(ItemsTable).rows_by_key) == [ids[0]] and app.semantic is None
        assert embedder.calls == [] and RunsRepo(app.conn).last("embed") is None


async def test_ask_before_any_vector_exists_is_refused_and_spends_nothing(make_app):
    embedder = HashEmbedder(64)
    llm = ScriptedLlm(replies=["Kernels [1]."])
    app = make_app(embedder=embedder, resolver=fake_resolver(llm))
    await _keyword_only(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("A")
        await until(pilot, lambda: _toast(app, "Ask needs the search index") is not None)
        assert "no passage has a vector yet" in (_toast(app, "Ask needs the search index") or "")
        assert not app.query_one(AskDrawer).display and app.mode == "NORMAL"
        assert embedder.calls == [] and llm.requests == []
        assert RunsRepo(app.conn).last("ask") is None
