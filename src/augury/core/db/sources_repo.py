import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from augury.core.clock import from_iso, to_iso
from augury.core.models import RECIPE_ADAPTER, FetchState, Recipe, Source

Health = Literal["never", "ok", "degraded", "broken"]
DEGRADED_AFTER = 1  # consecutive failures (spec §4.4): 1-2 is degraded (⚠)
BROKEN_AFTER = 3  # 3 or more is broken (✗, offers re-discover); never disabled automatically
# Where each user recipe keeps the URL it fetches: two sources on one URL are duplicates.
RECIPE_URL_KEYS = ("feed_url", "sitemap_url", "listing_url")
MAX_ID_LEN = 63  # Source.id's pattern: a lowercase letter or digit, then up to 62 more


def health_state(consecutive_failures: int, last_success_at: datetime | None) -> Health:
    """The spec §4.4 state machine, derived from what each fetch records: never → ok →
    degraded → broken, and back to ok on the next success."""
    if consecutive_failures >= BROKEN_AFTER:
        return "broken"
    if consecutive_failures >= DEGRADED_AFTER:
        return "degraded"
    return "ok" if last_success_at else "never"


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
        return health_state(self.consecutive_failures, self.last_success_at)


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

    def _insert(
        self, verb: str, source: Source, now: datetime, discovery_run_id: str | None = None
    ) -> None:
        self.conn.execute(
            f"{verb} INTO sources (id, name, homepage, origin, recipe_type, recipe_json, trust,"
            " enabled, added_via, discovery_run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                discovery_run_id,
                to_iso(now),
            ),
        )

    def ensure(self, source: Source, *, now: datetime) -> None:
        self._insert("INSERT OR IGNORE", source, now)

    def add(self, source: Source, *, now: datetime, discovery_run_id: str | None = None) -> None:
        try:
            self._insert("INSERT", source, now, discovery_run_id)
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
        """`base`, or else `base-2`, `base-3`, …, with base cut to leave room for the suffix, so
        the id still fits Source.id (slugify already caps base at 63 characters)."""
        candidate, n = base, 2
        while self.get(candidate) is not None:
            suffix = f"-{n}"
            candidate, n = f"{base[: MAX_ID_LEN - len(suffix)].rstrip('-')}{suffix}", n + 1
        return candidate

    def find_by_feed_url(self, feed_url: str) -> str | None:
        return self.find_by_recipe_url(feed_url)

    def find_by_recipe_url(self, url: str) -> str | None:
        """The source already fetching `url` (as its feed, sitemap or listing page), if any."""
        where = " OR ".join(f"json_extract(recipe_json, '$.{key}') = ?" for key in RECIPE_URL_KEYS)
        row = self.conn.execute(
            f"SELECT id FROM sources WHERE {where} ORDER BY id LIMIT 1",
            (url,) * len(RECIPE_URL_KEYS),
        ).fetchone()
        return row[0] if row else None

    def replace_recipe(
        self, source_id: str, recipe: Recipe, *, discovery_run_id: str | None
    ) -> bool:
        """Re-discover (spec §4.4): the same source, items and id with a new recipe. Its health
        starts over, and its conditional-GET state is dropped (it belonged to the old URL)."""
        cur = self.conn.execute(
            "UPDATE sources SET recipe_type = ?, recipe_json = ?, added_via = 'discovery',"
            " discovery_run_id = ?, fetch_state_json = '{}', last_error = NULL,"
            " consecutive_failures = 0 WHERE id = ? AND origin = 'user'",
            (recipe.type, recipe.model_dump_json(), discovery_run_id, source_id),
        )
        return cur.rowcount == 1

    def discovery_run_id(self, source_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT discovery_run_id FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return row[0] if row else None

    def item_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT source_id, count(*) FROM items GROUP BY source_id")
        return {r[0]: int(r[1]) for r in rows}
