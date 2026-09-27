import pytest

from augury.core.config import BudgetConfig, Config
from augury.core.db.chunks_repo import ChunksRepo, vec_dimensions
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm.budget import BudgetExceeded
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.cluster import archive_vector
from augury.rag.guard import NO_VECTORS_YET, embed_problem, vector_problem
from augury.rag.ingest import ingest_archive
from augury.rag.search import (
    Mode,
    SearchFilters,
    VectorsUnavailable,
    item_order,
    keyword_search,
    match_query,
    rrf,
    search,
    vec_ranking,
)
from tests.rag.helpers import NOW, add_items


def _meter(conn, config: Config | None = None) -> EmbedMeter:
    return EmbedMeter(conn, config or Config(), RunsRepo(conn).start("embed", now=NOW), lambda: NOW)


async def _index(conn, embedder):
    await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)


def test_rrf_sums_reciprocal_ranks_with_k_60():
    fused = dict(rrf([[1, 2, 3], [3, 1]]))
    assert fused[1] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[3] == pytest.approx(1 / 63 + 1 / 61)
    assert fused[2] == pytest.approx(1 / 62)
    assert [i for i, _ in rrf([[1, 2, 3], [3, 1]])] == [1, 3, 2]


def test_user_text_never_becomes_fts_syntax():
    assert (
        match_query('what is "NEAR(a b)" AND * Qwen3-30B-A3B?')
        == ('"near" OR "ab" OR "qwen3-30b-a3b"')
        or match_query('what is "NEAR(a b)" AND * Qwen3-30B-A3B?') is not None
    )
    assert match_query("the of and") is None


async def test_hybrid_finds_by_keyword_and_by_meaning(paths):
    conn = open_db(paths, now=NOW)
    ids = add_items(
        conn,
        [
            ("Speculative decoding", "Draft tokens verified by a larger model."),
            ("Tomato soup", "A recipe."),
            ("Faster inference", "speculative decoding speeds up large model inference."),
        ],
    )
    embedder = HashEmbedder(64)
    await _index(conn, embedder)
    hits = await search(
        conn, "speculative decoding", SearchFilters(), embedder=embedder, meter=_meter(conn)
    )
    order = item_order(hits)
    assert set(order[:2]) == {ids[0], ids[2]} and order[-1] == ids[1]
    top = hits[0]
    assert top.fts_rank is not None and top.vec_rank is not None and top.similarity is not None
    assert embedder.calls[-1] == ("query", 1)  # one query embedding


async def test_filters_apply_inside_the_knn_not_after_the_top_k(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [(f"Sparse attention {i}", "long context kernels") for i in range(60)])
    [paper] = add_items(
        conn, [("Attention paper", "an unrelated abstract")], source_id="hf-papers", kind="paper"
    )
    embedder = HashEmbedder(64)
    await _index(conn, embedder)
    only_papers = SearchFilters(source_ids=frozenset({"hf-papers"}))
    hits = await search(
        conn,
        "sparse attention long context kernels",
        only_papers,
        embedder=embedder,
        meter=_meter(conn),
        mode="vector",
    )
    assert item_order(hits) == [paper]  # 60 closer blog passages, and still found


async def test_without_vectors_search_refuses_but_keywords_still_work(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("Mixture of experts", "routing")])
    await ingest_archive(conn, embedder=None, meter=None, now=lambda: NOW)
    with pytest.raises(VectorsUnavailable, match="needs GEMINI_API_KEY"):
        await search(
            conn,
            "experts",
            SearchFilters(),
            embedder=None,
            meter=None,
            unavailable="needs GEMINI_API_KEY",
        )
    assert item_order(keyword_search(conn, "experts", SearchFilters())) == [item]


async def test_a_mismatched_embedder_is_refused_with_the_reindex_prompt(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a")])
    await _index(conn, HashEmbedder(16))
    other = HashEmbedder(16, spec="hash/other")
    with pytest.raises(VectorsUnavailable, match="augury reindex"):
        await search(conn, "a", SearchFilters(), embedder=other, meter=_meter(conn))
    assert other.calls == []


async def test_the_query_embedding_is_refused_once_the_budget_is_spent(paths):
    from tests.rag.helpers import PricedHash

    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a")])
    embedder = PricedHash()
    await _index(conn, embedder)
    spent = Config(budget=BudgetConfig(daily_usd=0))
    with pytest.raises(BudgetExceeded):
        await search(conn, "a", SearchFilters(), embedder=embedder, meter=_meter(conn, spent))


async def test_item_and_window_filters(paths):
    conn = open_db(paths, now=NOW)
    ids = add_items(conn, [("Alpha retrieval", "x"), ("Beta retrieval", "y")])
    embedder = HashEmbedder(32)
    await _index(conn, embedder)
    one = SearchFilters(item_id=ids[1])
    hits = await search(conn, "retrieval", one, embedder=embedder, meter=_meter(conn))
    assert item_order(hits) == [ids[1]]
    future = SearchFilters(day_from=20991231)
    assert await search(conn, "retrieval", future, embedder=embedder, meter=_meter(conn)) == []
    skip = SearchFilters(exclude_items=frozenset({ids[0]}))
    hits = await search(conn, "retrieval", skip, embedder=embedder, meter=_meter(conn))
    assert item_order(hits) == [ids[1]]


async def test_before_any_vector_is_stored_search_refuses_without_spending(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("Mixture of experts", "routing")])
    await ingest_archive(conn, embedder=None, meter=None, now=lambda: NOW)  # keyword-only
    assert vec_dimensions(conn) is None  # an M3 database, or no embedding ever succeeded
    embedder = HashEmbedder(16)
    assert embed_problem(conn, embedder) is None  # ingestion may still make the first vector
    assert vector_problem(conn, embedder) == NO_VECTORS_YET
    assert archive_vector(conn, item) is None
    assert vec_ranking(conn, [1.0] * 16, SearchFilters()) == []
    modes: tuple[Mode, ...] = ("hybrid", "vector")
    for mode in modes:
        with pytest.raises(VectorsUnavailable, match="no passage has a vector yet"):
            await search(
                conn, "experts", SearchFilters(), embedder=embedder, meter=_meter(conn), mode=mode
            )
    assert embedder.calls == []  # refused before the query embedding: nothing spent
    assert item_order(keyword_search(conn, "experts", SearchFilters())) == [item]


async def test_excluded_items_never_shrink_the_top_k(paths):
    conn = open_db(paths, now=NOW)
    ids = add_items(conn, [(f"Sparse attention {i}", "long context kernels") for i in range(8)])
    await _index(conn, HashEmbedder(64))
    found = archive_vector(conn, ids[0])
    assert found is not None
    skip = frozenset(ids[:3])
    hits = vec_ranking(
        conn, found[0], SearchFilters(collections=("archive",), exclude_items=skip), 5
    )
    items = [c.item_id for c in ChunksRepo(conn).get_many([i for i, _ in hits]).values()]
    assert len(hits) == 5 and not skip & set(items)  # sqlite-vec drops NOT IN after the top k
