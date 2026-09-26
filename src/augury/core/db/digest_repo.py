import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from augury.core.db.connect import transaction


@dataclass(frozen=True)
class DigestRow:
    day: str
    item_id: str
    position: int
    final_score: float
    breakdown: dict[str, Any]


class DigestRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace_day(self, day: date, rows: Sequence[tuple[str, int, float, str]]) -> None:
        """Rebuild a day's digest. Rows are (item_id, position, final_score, breakdown_json)."""
        with transaction(self.conn):
            self.conn.execute("DELETE FROM digests WHERE day = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT INTO digests (day, item_id, position, final_score, breakdown_json)"
                " VALUES (?, ?, ?, ?, ?)",
                [(day.isoformat(), *row) for row in rows],
            )

    def for_day(self, day: date) -> list[DigestRow]:
        rows = self.conn.execute(
            "SELECT * FROM digests WHERE day = ? ORDER BY position", (day.isoformat(),)
        )
        return [
            DigestRow(
                r["day"],
                r["item_id"],
                r["position"],
                r["final_score"],
                json.loads(r["breakdown_json"]),
            )
            for r in rows
        ]
