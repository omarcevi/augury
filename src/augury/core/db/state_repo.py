import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from augury.core.clock import from_iso, local_day, to_iso
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.connect import transaction

Toggle = Literal["liked", "saved", "hidden"]
_ACTIONS: dict[str, tuple[str, str]] = {
    "liked": ("like", "unlike"),
    "saved": ("save", "unsave"),
    "hidden": ("hide", "unhide"),
}


@dataclass(frozen=True)
class ItemState:
    read_at: datetime | None = None
    read_progress: float = 0.0
    liked: bool = False
    saved: bool = False
    hidden: bool = False


class StateRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, item_id: str) -> ItemState:
        row = self.conn.execute("SELECT * FROM item_state WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return ItemState()
        return ItemState(
            from_iso(row["read_at"]) if row["read_at"] else None,
            row["read_progress"],
            bool(row["liked"]),
            bool(row["saved"]),
            bool(row["hidden"]),
        )

    def _ensure(self, item_id: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO item_state (item_id, updated_at) VALUES (?, ?)",
            (item_id, to_iso(now)),
        )

    def log(self, item_id: str, action: str, *, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO interactions (item_id, action, at) VALUES (?, ?, ?)",
            (item_id, action, to_iso(now)),
        )

    def toggle(self, item_id: str, field: Toggle, *, now: datetime) -> bool:
        if field not in _ACTIONS:  # the column name is interpolated below, so whitelist it
            raise ValueError(f"unknown state field {field!r}")
        with transaction(self.conn):
            self._ensure(item_id, now)
            current = self.conn.execute(
                f"SELECT {field} FROM item_state WHERE item_id = ?", (item_id,)
            ).fetchone()[0]
            new = not bool(current)
            self.conn.execute(
                f"UPDATE item_state SET {field} = ?, updated_at = ? WHERE item_id = ?",
                (int(new), to_iso(now), item_id),
            )
            self.log(item_id, _ACTIONS[field][0 if new else 1], now=now)
        return new

    def mark_opened(self, item_id: str, *, now: datetime) -> None:
        with transaction(self.conn):
            self._ensure(item_id, now)
            row = self.conn.execute("SELECT read_at FROM item_state WHERE item_id = ?", (item_id,))
            first_read = row.fetchone()[0] is None
            self.conn.execute(
                "UPDATE item_state SET read_at = COALESCE(read_at, ?), updated_at = ?"
                " WHERE item_id = ?",
                (to_iso(now), to_iso(now), item_id),
            )
            self.log(item_id, "open", now=now)
            if first_read:  # M4: novelty reads this from the vector index (spec §6.4)
                ChunksRepo(self.conn).set_read_day(item_id, local_day(now))

    def set_progress(self, item_id: str, progress: float, *, now: datetime) -> None:
        progress = min(1.0, max(0.0, progress))
        with transaction(self.conn):
            self._ensure(item_id, now)
            self.conn.execute(
                "UPDATE item_state SET read_progress = MAX(read_progress, ?),"
                " updated_at = ? WHERE item_id = ?",
                (progress, to_iso(now), item_id),
            )

    def interactions(self, item_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT action FROM interactions WHERE item_id = ? ORDER BY id", (item_id,)
        )
        return [r[0] for r in rows]
