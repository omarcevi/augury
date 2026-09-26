import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from augury.core.clock import to_iso
from augury.core.db.connect import transaction
from augury.core.db.items_repo import row_to_item
from augury.core.models import Item, TriageResult


class TriageRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def untriaged(self, start: datetime, end: datetime) -> list[Item]:
        """Items first seen in [start, end) that have no triage result yet."""
        rows = self.conn.execute(
            "SELECT i.* FROM items i LEFT JOIN triage t ON t.item_id = i.id"
            " WHERE t.item_id IS NULL AND i.first_seen >= ? AND i.first_seen < ? ORDER BY i.pk",
            (to_iso(start), to_iso(end)),
        ).fetchall()
        return [row_to_item(r) for r in rows]

    def save_all(
        self, results: Iterable[TriageResult], *, run_id: str, model: str, prompt_version: int
    ) -> None:
        with transaction(self.conn):
            for r in results:
                self.conn.execute(
                    "INSERT INTO triage (item_id, run_id, relevance, why_read, tags_json,"
                    " flags_json, model, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (item_id) DO UPDATE SET run_id = excluded.run_id,"
                    " relevance = excluded.relevance, why_read = excluded.why_read,"
                    " tags_json = excluded.tags_json, flags_json = excluded.flags_json,"
                    " model = excluded.model, prompt_version = excluded.prompt_version",
                    (
                        r.item_id,
                        run_id,
                        r.relevance,
                        r.why_read,
                        json.dumps(r.tags),
                        json.dumps(r.flags),
                        model,
                        prompt_version,
                    ),
                )

    def get(self, item_id: str) -> TriageResult | None:
        row = self.conn.execute("SELECT * FROM triage WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return TriageResult(
            item_id=row["item_id"],
            relevance=row["relevance"],
            why_read=row["why_read"],
            tags=json.loads(row["tags_json"]),
            flags=json.loads(row["flags_json"]),
        )
