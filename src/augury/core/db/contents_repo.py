import sqlite3

from augury.core.clock import from_iso, to_iso
from augury.core.models import Content


class ContentsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, item_id: str) -> Content | None:
        row = self.conn.execute("SELECT * FROM contents WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return Content(
            item_id=row["item_id"],
            status=row["status"],
            body_md=row["body_md"],
            extractor=row["extractor"],
            extractor_version=row["extractor_version"],
            word_count=row["word_count"],
            error=row["error"],
            fetched_at=from_iso(row["fetched_at"]),
        )

    def save(self, c: Content) -> None:
        self.conn.execute(
            "INSERT INTO contents (item_id, body_md, extractor, extractor_version, word_count,"
            " status, error, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (item_id) DO"
            " UPDATE SET body_md = excluded.body_md, extractor = excluded.extractor,"
            " extractor_version = excluded.extractor_version, word_count = excluded.word_count,"
            " status = excluded.status, error = excluded.error, fetched_at = excluded.fetched_at",
            (
                c.item_id,
                c.body_md,
                c.extractor,
                c.extractor_version,
                c.word_count,
                c.status,
                c.error,
                to_iso(c.fetched_at),
            ),
        )
