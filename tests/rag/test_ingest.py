from typing import Literal

from augury.core.config import BudgetConfig, Config
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.models import Content
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.guard import vector_problem
from augury.rag.ingest import ingest_archive, ingest_content
from tests.rag.helpers import NOW, BrokenEmbedder, PricedHash, add_items


def _meter(conn, config: Config | None = None) -> EmbedMeter:
    run_id = RunsRepo(conn).start("scout", now=NOW)
    return EmbedMeter(conn, config or Config(), run_id, lambda: NOW)


def _archive(conn):
    return {c.item_id: c for c in _all(conn, "archive")}


def _all(conn, collection):
    repo = ChunksRepo(conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE collection = ?", (collection,))]
    return list(repo.get_many(ids).values())


async def test_every_item_gets_one_embedded_archive_passage(paths):
    conn = open_db(paths, now=NOW)
    ids = add_items(conn, [("Sparse attention", "Long context."), ("Diffusion", "Images.")])
    embedder = HashEmbedder(dimensions=16)
    stats = await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    assert (stats.archived, stats.embedded, stats.keyword_only) == (2, 2, None)
    archive = _archive(conn)
    assert set(archive) == set(ids)
    passage = archive[ids[0]]
    assert passage.text == "Sparse attention\nLong context.\nSource: Hugging Face Blog"
    assert passage.context_header == "Hugging Face Blog · Sparse attention"
    assert (passage.embed_model, passage.embed_dim) == (embedder.spec, 16)
    again = await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    assert again.archived == 0 and embedder.calls == [("document", 2)]  # nothing changed


async def test_a_changed_summary_or_new_tags_re_ingest_the_item(paths):
    conn = open_db(paths, now=NOW)
    [item] = add_items(conn, [("Title", "")])
    embedder = HashEmbedder(dimensions=16)
    await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    ItemsRepo(conn).set_enrichment(item, "An enriched opening paragraph.", now=NOW)
    conn.execute(
        "INSERT INTO triage (item_id, run_id, relevance, tags_json, model, prompt_version)"
        " VALUES (?, 'r', 7, '[\"agents\"]', 'm', 1)",
        (item,),
    )
    stats = await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    assert stats.archived == 1
    assert _archive(conn)[item].text.endswith("Tags: agents")


async def test_without_an_embedder_passages_are_keyword_only_until_one_is_set(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a"), ("B", "b")])
    stats = await ingest_archive(
        conn, embedder=None, meter=None, now=lambda: NOW, unavailable="needs GEMINI_API_KEY"
    )
    assert (stats.archived, stats.embedded) == (2, 0)
    assert stats.keyword_only == "needs GEMINI_API_KEY" and not stats.needs_reindex
    assert all(c.embed_model is None for c in _archive(conn).values())
    later = await ingest_archive(
        conn, embedder=HashEmbedder(16), meter=_meter(conn), now=lambda: NOW
    )
    assert later.embedded == 2  # stale: they had no vector


async def test_the_mixed_model_guard_keeps_another_model_out_of_the_index(paths):
    conn = open_db(paths, now=NOW)
    [first] = add_items(conn, [("A", "a")])
    await ingest_archive(conn, embedder=HashEmbedder(16), meter=_meter(conn), now=lambda: NOW)
    add_items(conn, [("B", "b")])
    other = HashEmbedder(16, spec="hash/other")
    stats = await ingest_archive(conn, embedder=other, meter=_meter(conn), now=lambda: NOW)
    assert stats.needs_reindex and "augury reindex" in (stats.keyword_only or "")
    assert stats.embedded == 0 and other.calls == []
    assert ChunksRepo(conn).index_models() == [("hash/feature-hashing", 16)]
    assert vector_problem(conn, other) == stats.keyword_only
    assert _archive(conn)[first].embed_model == "hash/feature-hashing"


async def test_the_budget_stops_embedding_but_passages_are_still_stored(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a")])
    config = Config(budget=BudgetConfig(daily_usd=0))
    embedder = PricedHash()
    stats = await ingest_archive(
        conn, embedder=embedder, meter=_meter(conn, config), now=lambda: NOW
    )
    assert stats.stopped and "budget" in stats.stopped
    assert (stats.archived, stats.embedded) == (1, 0) and embedder.calls == []


async def test_a_provider_error_is_recorded_not_raised(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a")])
    stats = await ingest_archive(
        conn, embedder=BrokenEmbedder(16), meter=_meter(conn), now=lambda: NOW
    )
    assert stats.error == "RuntimeError: provider down" and stats.archived == 1


def _content(
    item_id: str, body: str, status: Literal["ok", "failed", "paywalled"] = "ok"
) -> Content:
    return Content(
        item_id=item_id,
        status=status,
        body_md=body,
        extractor="html",
        extractor_version=1,
        fetched_at=NOW,
    )


async def test_content_passages_follow_the_full_text(paths):
    conn = open_db(paths, now=NOW)
    [item_id] = add_items(conn, [("Paper", "s")])
    item = ItemsRepo(conn).get(item_id)
    assert item is not None
    body = "# Method\n\nWe prune heads.\n\n# Results\n\nIt is faster.\n"
    embedder = HashEmbedder(16)
    stats = await ingest_content(
        conn,
        item,
        _content(item_id, body),
        embedder=embedder,
        meter=_meter(conn),
        now=lambda: NOW,
    )
    assert (stats.content, stats.embedded) == (2, 2)
    passages = ChunksRepo(conn).for_item(item_id, "content")
    assert [p.section for p in passages] == ["Method", "Results"]
    assert passages[0].context_header == "Hugging Face Blog · Paper · §Method"
    same = await ingest_content(
        conn,
        item,
        _content(item_id, body),
        embedder=embedder,
        meter=_meter(conn),
        now=lambda: NOW,
    )
    assert same.content == 0 and len(embedder.calls) == 1  # unchanged: nothing re-embedded
    failed = await ingest_content(
        conn,
        item,
        _content(item_id, "", "failed"),
        embedder=embedder,
        meter=_meter(conn),
        now=lambda: NOW,
    )
    assert failed.content == 0 and len(ChunksRepo(conn).for_item(item_id, "content")) == 2


async def test_a_backfill_the_budget_stops_carries_on_next_time(paths):
    conn = open_db(paths, now=NOW)
    add_items(conn, [(f"Item {i}", f"summary {i}") for i in range(250)])
    embedder = PricedHash()  # 100 tokens a passage at $0.15/Mtok: $0.0015 a batch of 100
    one_batch = Config(budget=BudgetConfig(daily_usd=0.0015))
    first = await ingest_archive(
        conn, embedder=embedder, meter=_meter(conn, one_batch), now=lambda: NOW
    )
    assert (first.archived, first.embedded) == (250, 100) and first.stopped
    assert ChunksRepo(conn).count("archive") == 250  # the rest is keyword-indexed meanwhile
    later = await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    assert (later.archived, later.embedded) == (150, 150)


async def test_without_sqlite_vec_everything_is_keyword_only(paths, monkeypatch):
    from augury.rag import guard

    monkeypatch.setattr(guard, "vec_loaded", lambda conn: False)
    conn = open_db(paths, now=NOW)
    add_items(conn, [("A", "a")])
    embedder = HashEmbedder(16)
    stats = await ingest_archive(conn, embedder=embedder, meter=_meter(conn), now=lambda: NOW)
    assert stats.keyword_only == guard.NO_VEC and stats.embedded == 0 and embedder.calls == []
    assert ChunksRepo(conn).count("archive") == 1
