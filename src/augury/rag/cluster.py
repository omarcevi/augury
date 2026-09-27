"""Clusters and novelty (spec §6.5). Identity (same URL or arXiv id) already merged items in
Normalize; here items are *grouped*, never merged. Each newly archived item is linked to its
KNN neighbours (k = 5, cosine ≥ [rag] cluster_threshold) among archive items published within
±14 days, and a blog that names a paper's arXiv id always joins that paper. Links are merged
with union-find, and every member of a cluster gets the same items.cluster_id.

Novelty is 1 - the highest similarity to anything read in the last 60 days, a KNN constraint
on read_day. With nothing read in that window it is unavailable, and ranking hands its weight
to the other terms (spec §5.4)."""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from augury.core.clock import day_number
from augury.core.db.chunks_repo import VEC_TABLE, ChunksRepo, vec_dimensions, vec_loaded
from augury.rag.search import SearchFilters, vec_ranking

CLUSTER_K = 5
WINDOW_DAYS = 14
NOVELTY_DAYS = 60


@dataclass(frozen=True)
class Sibling:
    item_id: str
    title: str
    source_id: str


def _vectors_usable(conn: sqlite3.Connection) -> bool:
    """Stored vectors exist and all come from one model, so comparing them means something."""
    if not vec_loaded(conn) or vec_dimensions(conn) is None:
        return False
    return len(ChunksRepo(conn).index_models()) == 1


def archive_vector(conn: sqlite3.Connection, item_id: str) -> tuple[bytes, int] | None:
    """The item's archive vector and its published_day, or None (not embedded yet)."""
    row = conn.execute(
        f"SELECT v.embedding, c.published_day FROM chunks c JOIN {VEC_TABLE} v"
        " ON v.chunk_id = c.id WHERE c.item_id = ? AND c.collection = 'archive'",
        (item_id,),
    ).fetchone()
    return (bytes(row[0]), int(row[1])) if row else None


def _shift(day: int, days: int) -> int:
    d = date(day // 10_000, day // 100 % 100, day % 100) + timedelta(days=days)
    return day_number(d)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[str]]:
        found: dict[str, list[str]] = {}
        for x in self.parent:
            found.setdefault(self.find(x), []).append(x)
        return list(found.values())


def _neighbours(conn: sqlite3.Connection, item_id: str, threshold: float) -> list[str]:
    found = archive_vector(conn, item_id)
    if found is None:
        return []
    blob, day = found
    window = SearchFilters(
        collections=("archive",),
        day_from=_shift(day, -WINDOW_DAYS),
        day_to=_shift(day, WINDOW_DAYS),
        exclude_items=frozenset({item_id}),
    )
    close = [cid for cid, sim in vec_ranking(conn, blob, window, CLUSTER_K) if sim >= threshold]
    return [c.item_id for c in ChunksRepo(conn).get_many(close).values()]


def _arxiv_partners(conn: sqlite3.Connection, item_id: str) -> list[str]:
    """Items sharing this one's arXiv id with the other kind (a blog and its paper)."""
    rows = conn.execute(
        "SELECT o.id FROM items i JOIN items o ON o.arxiv_id = i.arxiv_id AND o.id != i.id"
        " AND o.kind != i.kind WHERE i.id = ? AND i.arxiv_id IS NOT NULL",
        (item_id,),
    )
    return [r[0] for r in rows]


def _members(conn: sqlite3.Connection, item_ids: Iterable[str]) -> dict[str, str | None]:
    ids = list(dict.fromkeys(item_ids))
    found: dict[str, str | None] = {}
    for start in range(0, len(ids), 500):
        part = ids[start : start + 500]
        marks = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT id, cluster_id FROM items WHERE id IN ({marks})", part):
            found[r[0]] = r[1]
    return found


def update_clusters(conn: sqlite3.Connection, item_ids: Sequence[str], *, threshold: float) -> int:
    """Link these (newly archived) items; returns how many items' cluster_id changed. Runs in
    the caller's transaction."""
    uf = _UnionFind()
    vectors = _vectors_usable(conn)
    for item_id in item_ids:
        uf.find(item_id)
        partners = _arxiv_partners(conn, item_id)
        if vectors:
            partners += _neighbours(conn, item_id, threshold)
        for other in partners:
            uf.union(item_id, other)
    current = _members(conn, uf.parent)
    for cluster in {c for c in current.values() if c}:  # existing clusters stay whole
        for (member,) in conn.execute("SELECT id FROM items WHERE cluster_id = ?", (cluster,)):
            uf.union(cluster, member)
            current.setdefault(member, cluster)
    changed = 0
    for group in uf.groups():
        members = [m for m in group if m in current]
        if len(members) < 2:
            continue
        existing = sorted(c for m in members if (c := current[m]))
        cluster_id = existing[0] if existing else min(members)
        for member in members:
            if current[member] != cluster_id:
                conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (cluster_id, member))
                changed += 1
    return changed


def cluster_siblings(conn: sqlite3.Connection, item_id: str) -> list[Sibling]:
    """ "Also covered by": the other items in this item's cluster, newest first."""
    rows = conn.execute(
        "SELECT o.id, o.title, o.source_id FROM items i JOIN items o"
        " ON o.cluster_id = i.cluster_id AND o.id != i.id"
        " WHERE i.id = ? AND i.cluster_id IS NOT NULL ORDER BY o.first_seen DESC, o.pk DESC",
        (item_id,),
    )
    return [Sibling(r[0], r[1], r[2]) for r in rows]


def novelty_scores(
    conn: sqlite3.Connection, item_ids: Sequence[str], *, today: date
) -> dict[str, float]:
    """Novelty for each item that has a vector and something read in the last 60 days to
    compare with; the others are left out (unavailable, not zero)."""
    if not item_ids or not _vectors_usable(conn):
        return {}
    since = day_number(today - timedelta(days=NOVELTY_DAYS))
    scores: dict[str, float] = {}
    for item_id in item_ids:
        found = archive_vector(conn, item_id)
        if found is None:
            continue
        read = SearchFilters(
            collections=("archive",), read_since=since, exclude_items=frozenset({item_id})
        )
        nearest = vec_ranking(conn, found[0], read, 1)
        if nearest:
            scores[item_id] = min(1.0, max(0.0, 1.0 - nearest[0][1]))
    return scores
