"""discovery_runs (spec §7): what each discovery run was asked, what it proposed, and what the
user chose. Candidates are stored as JSON so a run can be shown again without the model."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from augury.core.clock import from_iso, to_iso

DiscoveryStatus = Literal["running", "ok", "partial", "failed", "interrupted"]
InputKind = Literal["url", "name"]


@dataclass(frozen=True)
class DiscoveryRun:
    id: str
    query: str
    input_kind: str
    status: str
    tool_calls: int
    candidates: list[dict[str, Any]]
    chosen: list[str]
    explanation: str | None
    session_id: str | None
    tokens_in: int
    tokens_out: int
    started_at: datetime
    finished_at: datetime | None


class DiscoveryRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(
        self, run_id: str, query: str, input_kind: InputKind, *, session_id: str, now: datetime
    ) -> None:
        self.conn.execute(
            "INSERT INTO discovery_runs (id, query, input_kind, status, session_id, started_at)"
            " VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, query, input_kind, session_id, to_iso(now)),
        )

    def finish(
        self,
        run_id: str,
        status: DiscoveryStatus,
        *,
        tool_calls: int,
        candidates: list[dict[str, Any]],
        explanation: str | None,
        now: datetime,
    ) -> None:
        """Copies the run's tokens from its runs row, which the usage ledger kept up to date."""
        self.conn.execute(
            "UPDATE discovery_runs SET status = ?, tool_calls = ?, candidates_json = ?,"
            " explanation = ?, finished_at = ?,"
            " tokens_in = COALESCE((SELECT tokens_in FROM runs WHERE runs.id = ?), 0),"
            " tokens_out = COALESCE((SELECT tokens_out FROM runs WHERE runs.id = ?), 0)"
            " WHERE id = ?",
            (
                status,
                tool_calls,
                json.dumps(candidates),
                explanation,
                to_iso(now),
                run_id,
                run_id,
                run_id,
            ),
        )

    def set_chosen(self, run_id: str, chosen: list[str]) -> None:
        """The recipe hashes the user confirmed."""
        self.conn.execute(
            "UPDATE discovery_runs SET chosen_json = ? WHERE id = ?", (json.dumps(chosen), run_id)
        )

    def mark_running_as_interrupted(self, *, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE discovery_runs SET status = 'interrupted', finished_at = ?"
            " WHERE status = 'running'",
            (to_iso(now),),
        )
        return cur.rowcount

    def get(self, run_id: str) -> DiscoveryRun | None:
        row = self.conn.execute("SELECT * FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return DiscoveryRun(
            id=row["id"],
            query=row["query"],
            input_kind=row["input_kind"],
            status=row["status"],
            tool_calls=row["tool_calls"],
            candidates=json.loads(row["candidates_json"]),
            chosen=json.loads(row["chosen_json"]),
            explanation=row["explanation"],
            session_id=row["session_id"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            started_at=from_iso(row["started_at"]),
            finished_at=from_iso(row["finished_at"]) if row["finished_at"] else None,
        )
