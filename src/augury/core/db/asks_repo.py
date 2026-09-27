"""Ask history (spec §5.7, §7): each question, the answer as shown, and what it cited."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from augury.core.clock import from_iso, to_iso


@dataclass(frozen=True)
class AskRecord:
    id: int
    run_id: str | None
    scope: dict[str, Any]
    question: str
    answer: str
    citations: list[dict[str, Any]]
    model: str
    created_at: datetime


class AsksRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(
        self,
        *,
        run_id: str | None,
        scope: dict[str, Any],
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
        model: str,
        now: datetime,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO asks (run_id, scope_json, question, answer, citations_json, model,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                json.dumps(scope),
                question,
                answer,
                json.dumps(citations),
                model,
                to_iso(now),
            ),
        )
        return int(cur.lastrowid or 0)

    def recent(self, limit: int = 20) -> list[AskRecord]:
        rows = self.conn.execute("SELECT * FROM asks ORDER BY id DESC LIMIT ?", (limit,))
        return [
            AskRecord(
                r["id"],
                r["run_id"],
                json.loads(r["scope_json"]),
                r["question"],
                r["answer"],
                json.loads(r["citations_json"]),
                r["model"],
                from_iso(r["created_at"]),
            )
            for r in rows
        ]
