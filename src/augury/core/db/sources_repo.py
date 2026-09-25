import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from augury.core.clock import from_iso, to_iso
from augury.core.models import RECIPE_ADAPTER, FetchState, Source

Health = Literal["never", "ok", "degraded", "broken"]


class SourceExists(Exception):
    pass


class BuiltinSourceError(Exception):
    pass


@dataclass(frozen=True)
class SourceRecord:
    source: Source
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int

    @property
    def health(self) -> Health:
        if self.consecutive_failures >= 3:
            return "broken"
        if self.consecutive_failures >= 1:
            return "degraded"
        return "ok" if self.last_success_at else "never"


def _record(row: sqlite3.Row) -> SourceRecord:
    source = Source(
        id=row["id"],
        name=row["name"],
        homepage=row["homepage"],
        origin=row["origin"],
        recipe=RECIPE_ADAPTER.validate_json(row["recipe_json"]),
        enabled=bool(row["enabled"]),
        added_via=row["added_via"],
        trust=row["trust"],
    )
    last = row["last_success_at"]
    return SourceRecord(
        source, from_iso(last) if last else None, row["last_error"], row["consecutive_failures"]
    )


class SourcesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _insert(self, verb: str, source: Source, now: datetime) -> None:
        self.conn.execute(
            f"{verb} INTO sources (id, name, homepage, origin, recipe_type, recipe_json, trust,"
            " enabled, added_via, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.id,
                source.name,
                source.homepage,
                source.origin,
                source.recipe.type,
                source.recipe.model_dump_json(),
                source.trust,
                int(source.enabled),
                source.added_via,
                to_iso(now),
            ),
        )

    def ensure(self, source: Source, *, now: datetime) -> None:
        self._insert("INSERT OR IGNORE", source, now)

    def add(self, source: Source, *, now: datetime) -> None:
        try:
            self._insert("INSERT", source, now)
        except sqlite3.IntegrityError as e:
            raise SourceExists(f"a source with id {source.id!r} already exists") from e

    def get(self, source_id: str) -> SourceRecord | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _record(row) if row else None

    def list_all(self, *, enabled_only: bool = False) -> list[SourceRecord]:
        sql = "SELECT * FROM sources" + (" WHERE enabled = 1" if enabled_only else "")
        return [_record(r) for r in self.conn.execute(sql + " ORDER BY origin, id")]

    def set_enabled(self, source_id: str, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id)
        )
        return cur.rowcount == 1

    def remove(self, source_id: str) -> bool:
        record = self.get(source_id)
        if record is None:
            return False
        if record.source.origin == "builtin":
            raise BuiltinSourceError(f"{source_id} is built in: disable it instead of removing it")
        self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return True

    def fetch_state(self, source_id: str) -> FetchState:
        row = self.conn.execute(
            "SELECT fetch_state_json FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return FetchState.model_validate_json(row[0]) if row else FetchState()

    def record_success(self, source_id: str, state: FetchState, *, now: datetime) -> None:
        self.conn.execute(
            "UPDATE sources SET last_success_at = ?, last_error = NULL, consecutive_failures = 0,"
            " fetch_state_json = ? WHERE id = ?",
            (to_iso(now), state.model_dump_json(), source_id),
        )

    def record_failure(self, source_id: str, error: str, *, now: datetime) -> None:
        self.conn.execute(
            "UPDATE sources SET last_error = ?, consecutive_failures = consecutive_failures + 1"
            " WHERE id = ?",
            (f"{to_iso(now)} {error}", source_id),
        )

    def unique_id(self, base: str) -> str:
        candidate, n = base, 2
        while self.get(candidate) is not None:
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def find_by_feed_url(self, feed_url: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM sources WHERE json_extract(recipe_json, '$.feed_url') = ?", (feed_url,)
        ).fetchone()
        return row[0] if row else None
