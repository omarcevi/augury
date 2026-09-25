import json
import sqlite3
from datetime import date, datetime

from augury.core.clock import from_iso, local_day, to_iso
from augury.core.models import Item, NormalizedItem, Signals, is_old
from augury.core.text import content_hash


def _opt_iso(dt: datetime | None) -> str | None:
    return to_iso(dt) if dt else None


def row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source_id=row["source_id"],
        kind=row["kind"],
        title=row["title"],
        url=row["url"],
        canonical_url=row["canonical_url"],
        authors=json.loads(row["authors_json"]),
        published_at=from_iso(row["published_at"]) if row["published_at"] else None,
        summary=row["summary"],
        arxiv_id=row["arxiv_id"],
        image_url=row["image_url"],
        first_seen=from_iso(row["first_seen"]),
        last_seen=from_iso(row["last_seen"]),
        times_seen=row["times_seen"],
        content_hash=row["content_hash"],
        is_old=bool(row["is_old"]),
    )


class ItemsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, n: NormalizedItem, *, now: datetime) -> tuple[str, bool]:
        row = self.conn.execute(
            "SELECT id, summary, last_seen, first_seen, published_at, authors_json FROM items"
            " WHERE id = ? OR canonical_url = ? LIMIT 1",
            (n.id, n.canonical_url),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO items (id, source_id, kind, title, url, canonical_url, authors_json,"
                " published_at, summary, arxiv_id, image_url, first_seen, last_seen, times_seen,"
                " content_hash, is_old) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    n.id,
                    n.source_id,
                    n.kind,
                    n.title,
                    n.url,
                    n.canonical_url,
                    json.dumps(n.authors),
                    _opt_iso(n.published_at),
                    n.summary,
                    n.arxiv_id,
                    n.image_url,
                    to_iso(now),
                    to_iso(now),
                    n.content_hash,
                    int(is_old(n.published_at, now)),
                ),
            )
            return n.id, True
        summary = n.summary or row["summary"]  # an empty API summary never clobbers an enriched one
        new_day = local_day(from_iso(row["last_seen"])) != local_day(now)
        stored_published_at = from_iso(row["published_at"]) if row["published_at"] else None
        merged_published_at = stored_published_at or n.published_at
        first_seen = from_iso(row["first_seen"])
        stored_authors = json.loads(row["authors_json"])
        authors = stored_authors or n.authors  # authors fill in once; never overwritten after that
        self.conn.execute(
            "UPDATE items SET title = ?, summary = ?, content_hash = ?, last_seen = ?,"
            " times_seen = times_seen + ?, image_url = COALESCE(?, image_url),"
            " published_at = COALESCE(published_at, ?), arxiv_id = COALESCE(arxiv_id, ?),"
            " authors_json = ?, is_old = ?"
            " WHERE id = ?",
            (
                n.title,
                summary,
                content_hash(n.title, summary),
                to_iso(now),
                int(new_day),
                n.image_url,
                _opt_iso(n.published_at),
                n.arxiv_id,
                json.dumps(authors),
                # recomputed against the merged date: a backfilled published_at can flip this
                int(is_old(merged_published_at, first_seen)),
                row["id"],
            ),
        )
        return row["id"], False

    def record_signals(self, item_id: str, day: date, signals: Signals, rank: int | None) -> None:
        if rank is None and not any(v is not None for v in signals.model_dump().values()):
            return
        self.conn.execute(
            "INSERT INTO signals (item_id, observed_on, rank, upvotes, upvotes7d, github_stars,"
            " comments) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (item_id, observed_on) DO UPDATE"
            " SET rank = excluded.rank, upvotes = excluded.upvotes, upvotes7d = excluded.upvotes7d,"
            " github_stars = excluded.github_stars, comments = excluded.comments",
            (
                item_id,
                day.isoformat(),
                rank,
                signals.upvotes,
                signals.upvotes7d,
                signals.github_stars,
                signals.comments,
            ),
        )

    def get(self, item_id: str) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return row_to_item(row) if row else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM items").fetchone()[0])

    def needing_enrichment(self, limit: int) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE kind = 'article' AND summary = '' AND enriched_at IS NULL"
            " ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row_to_item(r) for r in rows]

    def set_enrichment(self, item_id: str, summary: str, *, now: datetime) -> None:
        """Record the attempt even when nothing was found, so a broken page is tried once."""
        row = self.conn.execute(
            "SELECT title, summary FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return
        new_summary = summary or row["summary"]
        self.conn.execute(
            "UPDATE items SET summary = ?, content_hash = ?, enriched_at = ? WHERE id = ?",
            (new_summary, content_hash(row["title"], new_summary), to_iso(now), item_id),
        )
