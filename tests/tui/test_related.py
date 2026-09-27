from textual.widgets import Static

from augury.core.config import Config
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.ingest import ingest_archive
from augury.rag.related import related_items
from augury.tui.widgets.items_table import ItemsTable
from tests.helpers import CountingHttp
from tests.rag.helpers import add_items
from tests.tui.conftest import NOW, until
from tests.unit.test_extract_html import GENERIC


async def _indexed(conn, embedder) -> list[str]:
    ids = add_items(
        conn,
        [
            ("Sparse attention kernels", "long context attention"),
            ("Sparse attention serving", "long context attention at scale"),
            ("[bold red]Attention[/] sinks", "long context attention tricks"),
            ("Tomato soup", "a recipe"),
        ],
        now=NOW,
    )
    meter = EmbedMeter(conn, Config(), RunsRepo(conn).start("scout", now=NOW), lambda: NOW)
    await ingest_archive(conn, embedder=embedder, meter=meter, now=lambda: NOW)
    return ids


async def test_related_leaves_out_the_item_and_its_cluster(paths):
    from augury.core.db.open import open_db

    conn = open_db(paths, now=NOW)
    ids = await _indexed(conn, HashEmbedder(64))
    conn.execute("UPDATE items SET cluster_id = ? WHERE id IN (?, ?)", (ids[0], ids[0], ids[1]))
    related = [r.item_id for r in related_items(conn, ids[0])]
    assert ids[0] not in related and ids[1] not in related  # itself, and "Also covered by"
    assert related[0] == ids[2] and len(related) <= 5


def _panel(app) -> Static:
    return app.query_one("#reader-related", Static)


async def test_opening_an_item_shows_related_and_indexes_its_full_text(make_app):
    embedder = HashEmbedder(64)
    url = "https://example.com/hf-blog/0-Sparse-attention-kernels"
    app = make_app(embedder=embedder, http=CountingHttp({url: GENERIC}))
    ids = await _indexed(app.conn, embedder)
    async with app.run_test(size=(160, 40)) as pilot:
        app.query_one(ItemsTable).select_key(ids[0])
        await pilot.press("enter")
        await until(pilot, lambda: _panel(app).display)
        assert "Sparse attention serving" in str(_panel(app).content)
        assert "[bold red]Attention[/] sinks" in str(_panel(app).content)  # literally
        await until(pilot, lambda: ChunksRepo(app.conn).for_item(ids[0], "content") != [])
        passages = ChunksRepo(app.conn).for_item(ids[0], "content")
        assert all(p.embed_model == embedder.spec for p in passages)
        await until(pilot, lambda: RunsRepo(app.conn).last("embed") is not None)
        await pilot.press("escape", "up")  # another row's preview: the panel goes
        await until(pilot, lambda: not _panel(app).display)


async def test_without_an_embedder_the_reader_has_no_related_panel(make_app):
    url = "https://example.com/hf-blog/0-Sparse-attention-kernels"
    app = make_app(http=CountingHttp({url: GENERIC}))  # no key: no embedder
    ids = add_items(app.conn, [("Sparse attention kernels", "x"), ("Other", "y")], now=NOW)
    async with app.run_test(size=(160, 40)) as pilot:
        app.query_one(ItemsTable).select_key(ids[0])
        await pilot.press("enter")
        await until(pilot, lambda: ChunksRepo(app.conn).for_item(ids[0], "content") != [])
        assert not _panel(app).display  # keyword-only passages, and no panel
        assert all(p.embed_model is None for p in ChunksRepo(app.conn).for_item(ids[0], "content"))


async def test_a_mismatched_index_says_to_reindex(make_app):
    app = make_app(embedder=HashEmbedder(64, spec="hash/other"))
    ids = await _indexed(app.conn, HashEmbedder(64))
    async with app.run_test(size=(160, 40)) as pilot:
        app.query_one(ItemsTable).select_key(ids[0])
        await pilot.press("enter")
        await until(pilot, lambda: _panel(app).display)
        assert "augury reindex" in str(_panel(app).content)


async def test_a_five_member_cluster_still_leaves_five_related(paths):
    from augury.core.db.open import open_db

    conn = open_db(paths, now=NOW)
    story = "sparse attention kernels for long context"
    ids = add_items(
        conn,
        [(f"Sparse attention kernels {n}", story) for n in range(5)]
        + [(f"Attention serving {n}", "long context attention at scale") for n in range(5)],
        now=NOW,
    )
    meter = EmbedMeter(conn, Config(), RunsRepo(conn).start("scout", now=NOW), lambda: NOW)
    await ingest_archive(conn, embedder=HashEmbedder(64), meter=meter, now=lambda: NOW)
    cluster = ids[:5]  # the item and 4 siblings: its nearest vectors
    conn.execute("UPDATE items SET cluster_id = ? WHERE id IN (?, ?, ?, ?, ?)", (ids[0], *cluster))
    related = [r.item_id for r in related_items(conn, ids[0])]
    assert sorted(related) == sorted(ids[5:])  # 5, none of them "Also covered by"
