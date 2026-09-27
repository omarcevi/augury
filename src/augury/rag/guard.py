"""Can vectors be made and searched right now? (spec §6.3, §11) Every RAG path asks first: no
embedder (none configured, no key), no sqlite-vec, or an index built by another model or size
-- the mixed-model guard, which refuses and prompts `augury reindex`. Searching also needs a
stored vector: chunks_vec is made by the first vector write (Ruling R1), so until then search,
Related and Ask are keyword-only or off, while ingestion goes on and makes that first vector."""

import sqlite3

from augury.core.db.chunks_repo import ChunksRepo, vec_dimensions, vec_loaded
from augury.llm.embedder import Embedder, index_problem

NO_VEC = "sqlite-vec can't be loaded in this Python, so search is keyword-only"
NO_VECTORS_YET = "no passage has a vector yet; the next scout embeds them"


def _problem(
    conn: sqlite3.Connection, embedder: Embedder | None, unavailable: str | None, *, search: bool
) -> str | None:
    if embedder is None:
        return unavailable or "no embedder is configured"
    if not vec_loaded(conn):
        return NO_VEC
    index = ChunksRepo(conn).index_models()
    if (problem := index_problem(index, embedder)) is not None:
        return problem
    if search and (not index or vec_dimensions(conn) is None):
        return NO_VECTORS_YET
    return None


def embed_problem(
    conn: sqlite3.Connection, embedder: Embedder | None, unavailable: str | None = None
) -> str | None:
    """Why passages can't be embedded right now, or None (ingestion's guard)."""
    return _problem(conn, embedder, unavailable, search=False)


def vector_problem(
    conn: sqlite3.Connection, embedder: Embedder | None, unavailable: str | None = None
) -> str | None:
    """Why the index can't be searched by vector right now, or None: embed_problem's reasons,
    or no vector stored yet. Checked before any query embedding, so a refusal spends nothing."""
    return _problem(conn, embedder, unavailable, search=True)
