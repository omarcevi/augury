"""The Related panel (spec §6.5): KNN on the item's archive vector, leaving out the item and its
cluster siblings (they are "Also covered by"), top 5. Stored vectors only: no model call."""

import sqlite3
from dataclasses import dataclass

from augury.core.db.chunks_repo import ChunksRepo
from augury.rag.cluster import archive_vector, cluster_siblings
from augury.rag.search import SearchFilters, vec_ranking

RELATED_K = 5


@dataclass(frozen=True)
class RelatedItem:
    item_id: str
    title: str
    source_id: str
    similarity: float


def related_items(
    conn: sqlite3.Connection, item_id: str, *, k: int = RELATED_K
) -> list[RelatedItem]:
    """Callers check rag.guard.vector_problem first (spec §11: off without a usable index)."""
    found = archive_vector(conn, item_id)
    if found is None:
        return []
    skip = {item_id, *(s.item_id for s in cluster_siblings(conn, item_id))}
    filters = SearchFilters(collections=("archive",), exclude_items=frozenset(skip))
    hits = vec_ranking(conn, found[0], filters, k)
    chunks = ChunksRepo(conn).get_many([chunk_id for chunk_id, _ in hits])
    titles = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT id, title FROM items WHERE id IN ({','.join('?' * len(chunks))})",
            [c.item_id for c in chunks.values()],
        )
    }
    return [
        RelatedItem(c.item_id, titles.get(c.item_id, ""), c.source_id, similarity)
        for chunk_id, similarity in hits
        if (c := chunks.get(chunk_id)) is not None
    ]
