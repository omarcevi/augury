"""Hybrid search (spec §6.4): FTS5 bm25 top 50 and vec0 KNN top 50, fused with Reciprocal Rank
Fusion (k = 60). Filters apply inside both queries -- metadata constraints in the KNN, the
same conditions joined on `chunks` for FTS -- never after the top k, which could leave no
evidence at all. The one exception is `exclude_items` on the vector side: sqlite-vec applies
`NOT IN` only after picking the k nearest, so vec_ranking over-fetches by the excluded items'
passages and drops them itself. One query embedding per search (RETRIEVAL_QUERY), under the
budget."""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import sqlite_vec

from augury.core.db.chunks_repo import VEC_TABLE, ChunksRepo, StoredChunk, has_vectors
from augury.llm.embedder import Embedder, EmbedMeter
from augury.rag.guard import vector_problem

FTS_K = 50
VEC_K = 50
RRF_K = 60
MAX_K = 4096  # sqlite-vec's limit on a KNN query's k
MAX_QUERY_TOKENS = 32
_TOKEN = re.compile(r"[\w.\-]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    ]
)

Mode = Literal["hybrid", "fts", "vector"]


class VectorsUnavailable(Exception):
    """Vector search can't run (no embedder, no sqlite-vec, or the mixed-model guard)."""


@dataclass(frozen=True)
class SearchFilters:
    collections: tuple[str, ...] = ("archive", "content")
    item_id: str | None = None
    source_ids: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    day_from: int | None = None  # published_day window, yyyymmdd, inclusive
    day_to: int | None = None
    read_since: int | None = None  # read_day >= this (vector side only: FTS has no read_day)
    exclude_items: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Hit:
    chunk: StoredChunk
    score: float  # the fused RRF score
    fts_rank: int | None = None  # 1-based
    vec_rank: int | None = None
    similarity: float | None = None  # cosine, when the vector side found it


@dataclass
class _Clauses:
    sql: list[str] = field(default_factory=list)
    args: list[object] = field(default_factory=list)

    def add(self, sql: str, *args: object) -> None:
        self.sql.append(sql)
        self.args.extend(args)

    def among(self, column: str, values: Sequence[object], *, negate: bool = False) -> None:
        marks = ",".join("?" * len(values))
        self.add(f"{column} {'NOT IN' if negate else 'IN'} ({marks})", *values)


def _clauses(f: SearchFilters, prefix: str, *, vector: bool) -> _Clauses:
    c = _Clauses()
    c.among(f"{prefix}collection", list(f.collections))
    if f.item_id is not None:
        c.add(f"{prefix}item_id = ?", f.item_id)
    if f.source_ids:
        c.among(f"{prefix}source_id", sorted(f.source_ids))
    if f.kinds:
        c.among(f"{prefix}kind", sorted(f.kinds))
    if f.day_from is not None:
        c.add(f"{prefix}published_day >= ?", f.day_from)
    if f.day_to is not None:
        c.add(f"{prefix}published_day <= ?", f.day_to)
    if f.read_since is not None and vector:
        c.add(f"{prefix}read_day >= ?", f.read_since)
    if f.exclude_items and not vector:  # the vector side drops them itself (vec_ranking)
        c.among(f"{prefix}item_id", sorted(f.exclude_items), negate=True)
    return c


def match_query(text: str) -> str | None:
    """Any of the query's words, each quoted, so user text can never be FTS5 syntax."""
    seen: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        token = token.strip(".-")
        if len(token) >= 2 and token not in _STOPWORDS and token not in seen:
            seen.append(token)
    return " OR ".join(f'"{t}"' for t in seen[:MAX_QUERY_TOKENS]) or None


def fts_ranking(
    conn: sqlite3.Connection, query: str, filters: SearchFilters, limit: int = FTS_K
) -> list[int]:
    if (match := match_query(query)) is None:
        return []
    where = _clauses(filters, "c.", vector=False)
    rows = conn.execute(
        "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
        f" WHERE chunks_fts MATCH ? AND {' AND '.join(where.sql)}"
        " ORDER BY bm25(chunks_fts) LIMIT ?",
        [match, *where.args, limit],
    )
    return [int(r[0]) for r in rows]


def vec_ranking(
    conn: sqlite3.Connection,
    vector: Sequence[float] | bytes,
    filters: SearchFilters,
    limit: int = VEC_K,
) -> list[tuple[int, float]]:
    """(chunk id, cosine similarity), nearest first; every filter is a KNN constraint except
    `exclude_items`, which sqlite-vec would apply after the top k (so the excluded items'
    own vectors, usually the nearest, would take the places): k is raised by the excluded
    items' passages in scope and they are dropped here. Empty while there is no vector index."""
    if not has_vectors(conn):
        return []
    blob = vector if isinstance(vector, bytes) else sqlite_vec.serialize_float32(list(vector))
    where = _clauses(filters, "", vector=True)
    skip = filters.exclude_items
    k = min(limit + _passages_of(conn, skip, filters.collections), MAX_K)
    rows = conn.execute(
        f"SELECT chunk_id, item_id, distance FROM {VEC_TABLE} WHERE embedding MATCH ? AND k = ?"
        f" AND {' AND '.join(where.sql)} ORDER BY distance",
        [blob, k, *where.args],
    )
    kept = [(int(r[0]), 1.0 - float(r[2])) for r in rows if r[1] not in skip]
    return kept[:limit]


def _passages_of(
    conn: sqlite3.Connection, item_ids: frozenset[str], collections: Sequence[str]
) -> int:
    """How many passages these items have in these collections: at most that many vectors."""
    if not item_ids:
        return 0
    items, scope = sorted(item_ids), list(collections)
    row = conn.execute(
        f"SELECT count(*) FROM chunks WHERE item_id IN ({','.join('?' * len(items))})"
        f" AND collection IN ({','.join('?' * len(scope))})",
        [*items, *scope],
    ).fetchone()
    return int(row[0])


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: Σ 1 / (k + rank) over the rankings an id appears in."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def fuse(
    conn: sqlite3.Connection,
    fts_ids: Sequence[int],
    vec_hits: Sequence[tuple[int, float]],
    top_k: int,
) -> list[Hit]:
    vec_ids = [chunk_id for chunk_id, _ in vec_hits]
    similarity = dict(vec_hits)
    fts_rank = {chunk_id: n for n, chunk_id in enumerate(fts_ids, 1)}
    vec_rank = {chunk_id: n for n, chunk_id in enumerate(vec_ids, 1)}
    fused = rrf([ids for ids in (fts_ids, vec_ids) if ids])[:top_k]
    chunks = ChunksRepo(conn).get_many([chunk_id for chunk_id, _ in fused])
    return [
        Hit(chunks[i], score, fts_rank.get(i), vec_rank.get(i), similarity.get(i))
        for i, score in fused
        if i in chunks  # a vector whose passage is gone (pruned by the next scout)
    ]


def keyword_search(
    conn: sqlite3.Connection, query: str, filters: SearchFilters, *, top_k: int = 10
) -> list[Hit]:
    """FTS only: what search is without an embedder (spec N2)."""
    return fuse(conn, fts_ranking(conn, query, filters), [], top_k)


async def search(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters,
    *,
    embedder: Embedder | None,
    meter: EmbedMeter | None,
    top_k: int = 10,
    mode: Mode = "hybrid",
    unavailable: str | None = None,
) -> list[Hit]:
    """`hybrid` (the default), or one side alone (`fts`, `vector`) for the eval. Raises
    VectorsUnavailable when the vector side can't run: callers fall back or refuse."""
    if mode == "fts":
        return keyword_search(conn, query, filters, top_k=top_k)
    problem = vector_problem(conn, embedder, unavailable)
    if problem is not None or embedder is None or meter is None:
        raise VectorsUnavailable(problem or "no embedder")
    [vector] = await meter.embed(embedder, [query], "query")
    vec_hits = vec_ranking(conn, vector, filters)
    fts_ids = fts_ranking(conn, query, filters) if mode == "hybrid" else []
    return fuse(conn, fts_ids, vec_hits, top_k)


def item_order(hits: Sequence[Hit]) -> list[str]:
    """Each item once, where its best passage ranked (for listing items, not passages)."""
    order: list[str] = []
    for hit in hits:
        if hit.chunk.item_id not in order:
            order.append(hit.chunk.item_id)
    return order
