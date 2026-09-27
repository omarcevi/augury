"""Can vectors be made and searched right now? (spec §6.3, §11) Every RAG path asks this first:
no embedder (none configured, no key), no sqlite-vec, or an index built by another model or
size -- the mixed-model guard, which refuses and prompts `augury reindex`."""

import sqlite3

from augury.core.db.chunks_repo import ChunksRepo, vec_loaded
from augury.llm.embedder import Embedder, index_problem

NO_VEC = "sqlite-vec can't be loaded in this Python, so search is keyword-only"


def vector_problem(
    conn: sqlite3.Connection, embedder: Embedder | None, unavailable: str | None = None
) -> str | None:
    """Why passages can't be embedded or searched by vector right now, or None."""
    if embedder is None:
        return unavailable or "no embedder is configured"
    if not vec_loaded(conn):
        return NO_VEC
    return index_problem(ChunksRepo(conn).index_models(), embedder)
