import pytest

from augury.core.config import BudgetConfig, Config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm.budget import BudgetExceeded
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.ingest import ingest_archive
from augury.rag.search import (
    SearchFilters,
    VectorsUnavailable,
    item_order,
    keyword_search,
    match_query,
    rrf,
    search,
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
