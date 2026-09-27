"""The chunk store (spec §6.4). `chunks` is the source of truth; `chunks_fts` follows it by
trigger (migration 005) and `chunks_vec`, the sqlite-vec index, is written here in the same
transaction. chunks_vec is created on first use with the embedder's size, because migrations
can't know it and must not need the extension. `read_day` lives in the vector index so that
"read in the last 60 days" is a KNN constraint; it is set by deleting and re-inserting the
item's archive row (no in-place vec0 updates)."""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import sqlite_vec

from augury.core.clock import day_number, from_iso, local_day, to_iso
from augury.core.db.connect import vec_version

VEC_TABLE = "chunks_vec"
_DIMS = re.compile(r"float\[(\d+)\]")
_CHUNK_COLUMNS = (
    "item_id, collection, source_id, kind, trust, published_day, section, page, char_start,"
    " char_end, text, context_header, content_hash, embed_model, embed_dim, chunker_version,"
    " ingested_at"
)


@dataclass(frozen=True)
class NewChunk:
    item_id: str
    collection: str  # "archive" | "content"
    source_id: str
    kind: str
    trust: str
    published_day: int
    section: str
    char_start: int
    char_end: int
    text: str
    context_header: str
    content_hash: str
    chunker_version: int
    page: int | None = None

    @property
    def embed_text(self) -> str:
        return f"{self.context_header}\n\n{self.text}"


@dataclass(frozen=True)
class StoredChunk:
    id: int
    item_id: str
    collection: str
    source_id: str
    kind: str
    trust: str
    published_day: int
    section: str
    page: int | None
    text: str
    context_header: str
    embed_model: str | None
    embed_dim: int | None

    @property
    def embed_text(self) -> str:
        return f"{self.context_header}\n\n{self.text}"


def vec_loaded(conn: sqlite3.Connection) -> bool:
    return vec_version(conn) is not None


def vec_dimensions(conn: sqlite3.Connection) -> int | None:
    """The size chunks_vec was created with, or None when there is no vector index yet."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (VEC_TABLE,)).fetchone()
    match = _DIMS.search(row[0]) if row and row[0] else None
    return int(match.group(1)) if match else None


def has_vectors(conn: sqlite3.Connection) -> bool:
    """chunks_vec exists and can be read. It is made by the first vector write (Ruling R1), so
    an M3 database, or one where no embedding has succeeded yet, has none: every reader of the
    vector index checks this first and finds nothing, rather than `no such table`."""
    return vec_loaded(conn) and vec_dimensions(conn) is not None


def create_vec_table(conn: sqlite3.Connection, dimensions: int) -> None:
    # Metadata columns are filterable inside the KNN query (spec §6.4); + columns are stored only.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding float[{int(dimensions)}] distance_metric=cosine,"
        " collection TEXT PARTITION KEY, item_id TEXT, source_id TEXT, kind TEXT, trust TEXT,"
        " published_day INTEGER, read_day INTEGER, +section TEXT, +page INTEGER)"
    )


def drop_vec_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")


def published_day(published_at: datetime | None, first_seen: datetime) -> int:
    return day_number(local_day(published_at or first_seen))


def read_day_of(read_at: str | None) -> int:
    return day_number(local_day(from_iso(read_at))) if read_at else 0


def _row(r: sqlite3.Row) -> StoredChunk:
    return StoredChunk(
        id=r["id"],
        item_id=r["item_id"],
        collection=r["collection"],
        source_id=r["source_id"],
        kind=r["kind"],
        trust=r["trust"],
        published_day=r["published_day"],
        section=r["section"],
        page=r["page"],
        text=r["text"],
        context_header=r["context_header"],
        embed_model=r["embed_model"],
        embed_dim=r["embed_dim"],
    )


class ChunksRepo:
    """Every write method runs inside the caller's transaction (core/db/connect.transaction)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def index_models(self) -> list[tuple[str, int]]:
        """Each (embed_model, embed_dim) that stored vectors came from: one when healthy."""
        rows = self.conn.execute(
            "SELECT DISTINCT embed_model, embed_dim FROM chunks WHERE embed_model IS NOT NULL"
        )
        return [(str(r[0]), int(r[1])) for r in rows]

    def count(self, collection: str | None = None) -> int:
        if collection is None:
            return int(self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
        row = self.conn.execute("SELECT count(*) FROM chunks WHERE collection = ?", (collection,))
        return int(row.fetchone()[0])

    def ensure_vec_table(self, dimensions: int) -> None:
        """chunks_vec at this size. One of another size with no stored vectors is replaced."""
        current = vec_dimensions(self.conn)
        if current == dimensions:
            return
        if current is not None:
            if self.index_models():
                raise ValueError(
                    f"the vector index holds {current}-dimension vectors: run `augury reindex`"
                )
            drop_vec_table(self.conn)
        create_vec_table(self.conn, dimensions)

    def _read_day(self, item_id: str) -> int:
        row = self.conn.execute(
            "SELECT read_at FROM item_state WHERE item_id = ?", (item_id,)
        ).fetchone()
        return read_day_of(row[0] if row else None)

    def _insert_vector(self, chunk_id: int, c: NewChunk | StoredChunk, vector: Sequence[float]):
        self.conn.execute(
            f"INSERT INTO {VEC_TABLE} (chunk_id, embedding, collection, item_id, source_id, kind,"
            " trust, published_day, read_day, section, page)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                sqlite_vec.serialize_float32(list(vector)),
                c.collection,
                c.item_id,
                c.source_id,
                c.kind,
                c.trust,
                c.published_day,
                self._read_day(c.item_id),
                c.section,
                c.page,
            ),
        )

    def _delete_vectors(self, chunk_ids: Sequence[int]) -> None:
        if not chunk_ids or vec_dimensions(self.conn) is None or not vec_loaded(self.conn):
            return
        for chunk_id in chunk_ids:
            self.conn.execute(f"DELETE FROM {VEC_TABLE} WHERE chunk_id = ?", (chunk_id,))

    def replace(
        self,
        item_id: str,
        collection: str,
        chunks: Sequence[NewChunk],
        vectors: Sequence[Sequence[float]] | None,
        *,
        embed_model: str | None,
        embed_dim: int | None,
        now: datetime,
    ) -> list[int]:
        """An item's passages of one collection, replaced whole: rows, FTS and vectors."""
        old = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE item_id = ? AND collection = ?", (item_id, collection)
            )
        ]
        self._delete_vectors(old)
        self.conn.execute(
            "DELETE FROM chunks WHERE item_id = ? AND collection = ?", (item_id, collection)
        )
        if vectors is not None:
            assert embed_dim is not None and len(vectors) == len(chunks)
            self.ensure_vec_table(embed_dim)
        ids: list[int] = []
        for n, c in enumerate(chunks):
            with_vector = vectors is not None
            cur = self.conn.execute(
                f"INSERT INTO chunks ({_CHUNK_COLUMNS}) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.item_id,
                    c.collection,
                    c.source_id,
                    c.kind,
                    c.trust,
                    c.published_day,
                    c.section,
                    c.page,
                    c.char_start,
                    c.char_end,
                    c.text,
                    c.context_header,
                    c.content_hash,
                    embed_model if with_vector else None,
                    embed_dim if with_vector else None,
                    c.chunker_version,
                    to_iso(now),
                ),
            )
            chunk_id = int(cur.lastrowid or 0)
            ids.append(chunk_id)
            if vectors is not None:
                self._insert_vector(chunk_id, c, vectors[n])
        return ids

    def set_vectors(
        self,
        rows: Sequence[tuple[StoredChunk, Sequence[float]]],
        *,
        embed_model: str,
        embed_dim: int,
    ) -> None:
        """Reindex: give stored passages new vectors (chunks_vec must exist at embed_dim)."""
        self._delete_vectors([c.id for c, _ in rows])
        for c, vector in rows:
            self._insert_vector(c.id, c, vector)
            self.conn.execute(
                "UPDATE chunks SET embed_model = ?, embed_dim = ? WHERE id = ?",
                (embed_model, embed_dim, c.id),
            )

    def clear_vectors(self) -> None:
        """Reindex from scratch: no passage has a vector any more."""
        drop_vec_table(self.conn)
        self.conn.execute("UPDATE chunks SET embed_model = NULL, embed_dim = NULL")

    def set_read_day(self, item_id: str, day: date) -> None:
        """First read (spec §6.4): the item's archive vector is deleted and re-inserted with
        read_day set, so novelty's "read in the last 60 days" filters inside the KNN."""
        if vec_dimensions(self.conn) is None or not vec_loaded(self.conn):
            return
        stored = {c.id: c for c in self.for_item(item_id, "archive") if c.embed_model is not None}
        for chunk_id, c in stored.items():
            row = self.conn.execute(
                f"SELECT embedding FROM {VEC_TABLE} WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if row is None:
                continue
            self.conn.execute(f"DELETE FROM {VEC_TABLE} WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute(
                f"INSERT INTO {VEC_TABLE} (chunk_id, embedding, collection, item_id, source_id,"
                " kind, trust, published_day, read_day, section, page)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    row[0],
                    c.collection,
                    c.item_id,
                    c.source_id,
                    c.kind,
                    c.trust,
                    c.published_day,
                    day_number(day),
                    c.section,
                    c.page,
                ),
            )

    def for_item(self, item_id: str, collection: str) -> list[StoredChunk]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE item_id = ? AND collection = ? ORDER BY char_start, id",
            (item_id, collection),
        )
        return [_row(r) for r in rows]

    def get_many(self, ids: Sequence[int]) -> dict[int, StoredChunk]:
        found: dict[int, StoredChunk] = {}
        for start in range(0, len(ids), 500):
            part = list(ids[start : start + 500])
            marks = ",".join("?" * len(part))
            for r in self.conn.execute(f"SELECT * FROM chunks WHERE id IN ({marks})", part):
                found[int(r["id"])] = _row(r)
        return found

    def needing_vectors(self, embed_model: str, embed_dim: int) -> list[int]:
        """Passages without a vector from this embedder at this size (reindex's work list)."""
        missing = ""
        if vec_dimensions(self.conn) is not None:
            missing = f" OR id NOT IN (SELECT chunk_id FROM {VEC_TABLE})"
        rows = self.conn.execute(
            "SELECT id FROM chunks WHERE embed_model IS NOT ? OR embed_dim IS NOT ?"
            f"{missing} ORDER BY id",
            (embed_model, embed_dim),
        )
        return [int(r[0]) for r in rows]

    def prune_vectors(self) -> int:
        """Vectors whose passage is gone (a removed source cascades its items and chunks, but
        a trigger can't reach chunks_vec without the extension): deleted."""
        if vec_dimensions(self.conn) is None or not vec_loaded(self.conn):
            return 0
        cur = self.conn.execute(
            f"DELETE FROM {VEC_TABLE} WHERE chunk_id NOT IN (SELECT id FROM chunks)"
        )
        return cur.rowcount
