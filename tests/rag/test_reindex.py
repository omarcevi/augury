from augury.core.config import BudgetConfig, Config
from augury.core.db.chunks_repo import ChunksRepo, vec_dimensions
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.guard import vector_problem
from augury.rag.ingest import ingest_archive
from augury.rag.reindex import reindex
from tests.rag.helpers import NOW, PricedHash, add_items


async def _indexed(paths, n: int = 3, dims: int = 16):
    conn = open_db(paths, now=NOW)
    add_items(conn, [(f"Title {i}", f"Summary {i}") for i in range(n)])
    run_id = RunsRepo(conn).start("scout", now=NOW)
    meter = EmbedMeter(conn, Config(), run_id, lambda: NOW)
    await ingest_archive(conn, embedder=HashEmbedder(dims), meter=meter, now=lambda: NOW)
    return conn


async def test_a_new_embedder_size_recreates_the_index_and_clears_the_guard(paths):
    conn = await _indexed(paths)
    new = HashEmbedder(32, spec="hash/bigger")
    assert vector_problem(conn, new) is not None  # refused until reindexed
    stats = await reindex(conn, config=Config(), embedder=new, now=lambda: NOW)
    assert stats.recreated and (stats.chunks, stats.embedded, stats.remaining) == (3, 3, 0)
    assert vec_dimensions(conn) == 32 and vector_problem(conn, new) is None
    assert ChunksRepo(conn).index_models() == [("hash/bigger", 32)]
    run = RunsRepo(conn).last("embed")
    assert run is not None and run.status == "ok"


async def test_reindex_carries_on_after_a_budget_stop(paths):
    conn = await _indexed(paths, n=150)
    priced = PricedHash(dimensions=16)  # a different model at the same size
    spent = Config(budget=BudgetConfig(daily_usd=0.0000150))  # room for one batch of 100
    first = await reindex(conn, config=spent, embedder=priced, now=lambda: NOW)
    assert first.stopped and (first.embedded, first.remaining) == (100, 50)
    assert not first.recreated and vector_problem(conn, priced) is not None  # still mixed
    assert RunsRepo(conn).last("embed").status == "partial"  # type: ignore[union-attr]
    second = await reindex(conn, config=Config(), embedder=priced, now=lambda: NOW)
    assert (second.embedded, second.remaining) == (50, 0)
    assert vector_problem(conn, priced) is None


async def test_without_an_embedder_only_the_keyword_index_is_rebuilt(paths):
    conn = await _indexed(paths)
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('delete-all')")
    stats = await reindex(
        conn, config=Config(), embedder=None, now=lambda: NOW, unavailable="embeddings are off"
    )
    assert stats.keyword_only == "embeddings are off" and stats.chunks == 3
    hits = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'Summary'")
    assert hits.fetchone()[0] == 3


async def test_everything_re_embeds_every_passage(paths):
    conn = await _indexed(paths)
    embedder = HashEmbedder(16)
    stats = await reindex(conn, config=Config(), embedder=embedder, now=lambda: NOW)
    assert stats.embedded == 0  # already current
    stats = await reindex(
        conn, config=Config(), embedder=embedder, now=lambda: NOW, everything=True
    )
    assert stats.recreated and stats.embedded == 3
