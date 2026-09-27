import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from augury.core.clock import from_iso, to_iso

RunKind = Literal["scout", "discovery", "summarize", "ask", "embed"]
RunStatus = Literal["running", "ok", "partial", "failed", "interrupted"]


@dataclass(frozen=True)
class RunRecord:
    id: str
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    stats: dict[str, Any]


@dataclass(frozen=True)
class Spend:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    unpriced_tokens: int = 0


class RunsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def start(self, kind: RunKind, *, now: datetime) -> str:
        run_id = f"{kind}-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
        self.conn.execute(
            "INSERT INTO runs (id, kind, started_at, status) VALUES (?, ?, ?, 'running')",
            (run_id, kind, to_iso(now)),
        )
        return run_id

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        now: datetime,
        error: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ?, stats_json = ? WHERE id = ?",
            (status, to_iso(now), error, json.dumps(stats or {}), run_id),
        )

    def mark_running_as_interrupted(self, kind: RunKind, *, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE kind = ? AND"
            " status = 'running'",
            (to_iso(now), kind),
        )
        return cur.rowcount

    def last(
        self, kind: RunKind, *, statuses: tuple[RunStatus, ...] | None = None
    ) -> RunRecord | None:
        sql, args = "SELECT * FROM runs WHERE kind = ?", [kind]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        row = self.conn.execute(
            sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", args
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            row["id"],
            row["kind"],
            row["status"],
            from_iso(row["started_at"]),
            from_iso(row["finished_at"]) if row["finished_at"] else None,
            row["error"],
            json.loads(row["stats_json"]),
        )

    def add_usage(
        self,
        run_id: str,
        *,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        unpriced_tokens: int,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?,"
            " cost_usd = cost_usd + ?, unpriced_tokens = unpriced_tokens + ? WHERE id = ?",
            (tokens_in, tokens_out, cost_usd, unpriced_tokens, run_id),
        )

    def spend_between(self, start: datetime, end: datetime) -> Spend:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0),"
            " COALESCE(SUM(cost_usd), 0), COALESCE(SUM(unpriced_tokens), 0)"
            " FROM runs WHERE started_at >= ? AND started_at < ?",
            (to_iso(start), to_iso(end)),
        ).fetchone()
        return Spend(int(row[0]), int(row[1]), float(row[2]), int(row[3]))
