import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from augury.core.clock import from_iso, to_iso


@dataclass(frozen=True)
class Summary:
    item_id: str
    prompt_version: int
    model: str
    tldr: list[str]
    takeaways: list[str]
    created_at: datetime


class SummariesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, item_id: str, *, prompt_version: int, model: str) -> Summary | None:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE item_id = ? AND prompt_version = ? AND model = ?",
            (item_id, prompt_version, model),
        ).fetchone()
        if row is None:
            return None
        body = json.loads(row["tldr_json"])
        return Summary(
            row["item_id"],
            row["prompt_version"],
            row["model"],
            list(body["tldr"]),
            list(body["takeaways"]),
            from_iso(row["created_at"]),
        )

    def save(self, summary: Summary) -> None:
        self.conn.execute(
            "INSERT INTO summaries (item_id, prompt_version, model, tldr_json, created_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (item_id, prompt_version, model)"
            " DO UPDATE SET tldr_json = excluded.tldr_json, created_at = excluded.created_at",
            (
                summary.item_id,
                summary.prompt_version,
                summary.model,
                json.dumps({"tldr": summary.tldr, "takeaways": summary.takeaways}),
                to_iso(summary.created_at),
            ),
        )
