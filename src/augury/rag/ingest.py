"""Ingestion tiers (spec §6.1). `archive`: one passage per item (title, summary, source, tags),
for every item, by the scout. `content`: the full text's passages, when it is extracted (the
scout's prefetch, opening an item, or Ask). Without a usable embedder -- none configured, no
key, sqlite-vec missing, or an index built by another model (the mixed-model guard) --
passages are still stored and keyword-indexed, just without vectors, and a later run (or
`augury reindex`) embeds them. Embedding stops at the budget like any model call."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from itertools import batched

from pydantic import BaseModel

from augury.agents.llm_step import describe, is_budget_stop
from augury.core.clock import from_iso
from augury.core.db.chunks_repo import ChunksRepo, NewChunk, published_day
from augury.core.db.connect import transaction
from augury.core.models import Content, Item
from augury.llm.embedder import EMBED_BATCH, Embedder, EmbedMeter
from augury.rag.chunker import CHUNKER_VERSION, archive_text, chunk_markdown, context_header
from augury.rag.cluster import update_clusters
from augury.rag.guard import vector_problem


class IngestStats(BaseModel):
    archived: int = 0  # archive passages written (new or changed items)
    content: int = 0  # content passages written
    embedded: int = 0  # passages that got a vector
    clustered: int = 0  # items whose cluster changed
    keyword_only: str | None = None  # why no vectors were made
    needs_reindex: bool = False  # the mixed-model guard refused: `augury reindex`
    stopped: str | None = None  # the budget (or the provider) stopped embedding
    error: str | None = None


def _hash(*parts: str) -> str:
    return hashlib.sha1("\n".join([str(CHUNKER_VERSION), *parts]).encode()).hexdigest()


def _mark(stats: IngestStats, problem: str | None) -> None:
    stats.keyword_only = problem
    stats.needs_reindex = problem is not None and "augury reindex" in problem


def stale_archive(conn: sqlite3.Connection, *, vectors: bool) -> list[NewChunk]:
    """Items whose archive passage is missing, changed (a new summary or tags), or has no
    vector while vectors can be made."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.summary, i.kind, i.source_id, i.published_at, i.first_seen,"
        " s.name AS source_name, s.trust, t.tags_json, c.content_hash AS stored, c.embed_model"
        " FROM items i JOIN sources s ON s.id = i.source_id"
        " LEFT JOIN triage t ON t.item_id = i.id"
        " LEFT JOIN chunks c ON c.item_id = i.id AND c.collection = 'archive'"
        " ORDER BY i.first_seen DESC, i.pk DESC"
    )
    todo: list[NewChunk] = []
    for r in rows:
        tags = json.loads(r["tags_json"]) if r["tags_json"] else []
        text = archive_text(r["title"], r["summary"], r["source_name"], tags)
        header = context_header(r["source_name"], r["title"])
        digest = _hash(header, text)
        if r["stored"] == digest and (not vectors or r["embed_model"] is not None):
            continue
        published = from_iso(r["published_at"]) if r["published_at"] else None
        todo.append(
            NewChunk(
                item_id=r["id"],
                collection="archive",
                source_id=r["source_id"],
                kind=r["kind"],
                trust=r["trust"],
                published_day=published_day(published, from_iso(r["first_seen"])),
                section="",
                char_start=0,
                char_end=len(text),
                text=text,
                context_header=header,
                content_hash=digest,
                chunker_version=CHUNKER_VERSION,
            )
        )
    return todo


async def _embed_or_stop(
    stats: IngestStats,
    embedder: Embedder | None,
    meter: EmbedMeter | None,
    texts: list[str],
) -> list[list[float]] | None:
    """Vectors, or None (keyword-only) after a budget or provider stop, recorded in stats."""
    if embedder is None or meter is None or stats.stopped or stats.error:
        return None
    try:
        return await meter.embed(embedder, texts, "document")
    except Exception as exc:
        if is_budget_stop(exc):
            stats.stopped = describe(exc, 200)
        else:
            stats.error = describe(exc, 300)
        return None


async def ingest_archive(
    conn: sqlite3.Connection,
    *,
    embedder: Embedder | None,
    meter: EmbedMeter | None,
    now: Callable[[], datetime],
    unavailable: str | None = None,
    cluster_threshold: float | None = None,
) -> IngestStats:
    """The scout's archive tier: every item's one passage, embedded 100 at a time. With
    `cluster_threshold`, each batch is then clustered (spec §6.5)."""
    stats = IngestStats()
    problem = vector_problem(conn, embedder, unavailable)
    _mark(stats, problem)
    usable = embedder if problem is None else None
    repo = ChunksRepo(conn)
    with transaction(conn):
        repo.prune_vectors()
    for batch in batched(
        stale_archive(conn, vectors=usable is not None), EMBED_BATCH, strict=False
    ):
        vectors = await _embed_or_stop(stats, usable, meter, [c.embed_text for c in batch])
        with transaction(conn):  # no await inside: the connection is shared
            for n, chunk in enumerate(batch):
                repo.replace(
                    chunk.item_id,
                    "archive",
                    [chunk],
                    [vectors[n]] if vectors is not None else None,
                    embed_model=usable.spec if usable and vectors is not None else None,
                    embed_dim=usable.dimensions if usable and vectors is not None else None,
                    now=now(),
                )
            if cluster_threshold is not None:
                ids = [c.item_id for c in batch]
                stats.clustered += update_clusters(conn, ids, threshold=cluster_threshold)
        stats.archived += len(batch)
        stats.embedded += len(batch) if vectors is not None else 0
    return stats


def _source(conn: sqlite3.Connection, source_id: str) -> tuple[str, str]:
    row = conn.execute("SELECT name, trust FROM sources WHERE id = ?", (source_id,)).fetchone()
    return (row["name"], row["trust"]) if row else (source_id, "publisher")


def content_needs_ingest(
    conn: sqlite3.Connection, item_id: str, content: Content, *, vectors: bool
) -> bool:
    """False when the item's content passages match this text and chunker, and have vectors
    (or can't get any now): nothing to write, and no run to start."""
    if content.status != "ok" or not content.body_md.strip():
        return False
    row = conn.execute(
        "SELECT content_hash, embed_model FROM chunks WHERE item_id = ? AND collection = 'content'"
        " LIMIT 1",
        (item_id,),
    ).fetchone()
    if row is None or row[0] != _hash(content.body_md):
        return True
    return vectors and row[1] is None


async def ingest_content(
    conn: sqlite3.Connection,
    item: Item,
    content: Content,
    *,
    embedder: Embedder | None,
    meter: EmbedMeter | None,
    now: Callable[[], datetime],
    unavailable: str | None = None,
) -> IngestStats:
    """The content tier for one extracted item. Nothing to do when its passages are current
    (same text, same chunker) and embedded, or when they can't be embedded anyway."""
    stats = IngestStats()
    if content.status != "ok" or not content.body_md.strip():
        return stats
    problem = vector_problem(conn, embedder, unavailable)
    _mark(stats, problem)
    usable = embedder if problem is None else None
    if not content_needs_ingest(conn, item.id, content, vectors=usable is not None):
        return stats
    source_name, trust = _source(conn, item.source_id)
    day = published_day(item.published_at, item.first_seen)
    chunks = [
        NewChunk(
            item_id=item.id,
            collection="content",
            source_id=item.source_id,
            kind=item.kind,
            trust=trust,
            published_day=day,
            section=c.section,
            char_start=c.char_start,
            char_end=c.char_end,
            text=c.text,
            context_header=c.context_header,
            content_hash=_hash(content.body_md),
            chunker_version=CHUNKER_VERSION,
        )
        for c in chunk_markdown(content.body_md, source_name=source_name, title=item.title)
    ]
    if not chunks:
        return stats
    vectors = await _embed_or_stop(stats, usable, meter, [c.embed_text for c in chunks])
    with transaction(conn):
        ChunksRepo(conn).replace(
            item.id,
            "content",
            chunks,
            vectors,
            embed_model=usable.spec if usable and vectors is not None else None,
            embed_dim=usable.dimensions if usable and vectors is not None else None,
            now=now(),
        )
    stats.content = len(chunks)
    stats.embedded = len(chunks) if vectors is not None else 0
    return stats
