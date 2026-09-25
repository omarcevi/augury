import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from augury.core.clock import from_iso, local_day, local_day_bounds, to_iso

DateRange = Literal["today", "7d", "30d", "all"]
SortKey = Literal["newest", "popular", "reading_time"]
ShowKey = Literal["unread", "all", "saved", "liked", "hidden"]
DATE_RANGES: tuple[DateRange, ...] = ("today", "7d", "30d", "all")
SORT_KEYS: tuple[SortKey, ...] = ("newest", "popular", "reading_time")
SHOW_KEYS: tuple[ShowKey, ...] = ("unread", "all", "saved", "liked", "hidden")
WORDS_PER_MINUTE = 230


@dataclass(frozen=True)
class ItemFilter:
    search: str = ""
    sources: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    date: DateRange = "today"
    sort: SortKey = "newest"
    show: ShowKey = "unread"


DIGEST_PRESET = ItemFilter()


@dataclass(frozen=True)
class ItemRow:
    id: str
    title: str
    source_id: str
    kind: str
    url: str
    published_at: datetime | None
    first_seen: datetime
    is_old: bool
    popularity: int | None
    word_count: int | None
    read_at: datetime | None
    read_progress: float
    liked: bool
    saved: bool
    hidden: bool

    @property
    def reading_minutes(self) -> int | None:
        return max(1, round(self.word_count / WORDS_PER_MINUTE)) if self.word_count else None

    @property
    def read_state(self) -> Literal["unread", "partial", "read"]:
        if self.read_at is None:
            return "unread"
        return "read" if self.read_progress >= 0.9 else "partial"


_SELECT = """
SELECT i.id, i.title, i.source_id, i.kind, i.url, i.published_at, i.first_seen, i.is_old,
       COALESCE(sig.upvotes7d, sig.upvotes) AS popularity, c.word_count,
       st.read_at, COALESCE(st.read_progress, 0) AS read_progress,
       COALESCE(st.liked, 0) AS liked, COALESCE(st.saved, 0) AS saved,
       COALESCE(st.hidden, 0) AS hidden
FROM items i
LEFT JOIN item_state st ON st.item_id = i.id
LEFT JOIN contents c ON c.item_id = i.id AND c.status = 'ok'
LEFT JOIN signals sig ON sig.item_id = i.id
     AND sig.observed_on = (SELECT MAX(s2.observed_on) FROM signals s2 WHERE s2.item_id = i.id)
"""
_SHOW = {
    "unread": "st.read_at IS NULL AND COALESCE(st.hidden, 0) = 0",
    "all": "COALESCE(st.hidden, 0) = 0",
    "saved": "st.saved = 1 AND COALESCE(st.hidden, 0) = 0",
    "liked": "st.liked = 1 AND COALESCE(st.hidden, 0) = 0",
    "hidden": "st.hidden = 1",
}
_ORDER = {
    "newest": "i.first_seen DESC, COALESCE(i.published_at, i.first_seen) DESC, i.pk DESC",
    "popular": "popularity IS NULL, popularity DESC, i.first_seen DESC",
    "reading_time": "c.word_count IS NULL, c.word_count ASC, i.first_seen DESC",
}


def fts_query(text: str) -> str | None:
    """Every token quoted and prefix-matched, so user input can never be FTS syntax."""
    tokens = [t.replace('"', "") for t in re.findall(r"[\w.\-]+", text)]
    return " AND ".join(f'"{t}"*' for t in tokens if t) or None


def date_floor(date: DateRange, now: datetime) -> datetime | None:
    if date == "today":
        return local_day_bounds(local_day(now))[0]
    if date == "all":
        return None
    return now - timedelta(days=7 if date == "7d" else 30)


def _row(r: sqlite3.Row) -> ItemRow:
    return ItemRow(
        id=r["id"],
        title=r["title"],
        source_id=r["source_id"],
        kind=r["kind"],
        url=r["url"],
        published_at=from_iso(r["published_at"]) if r["published_at"] else None,
        first_seen=from_iso(r["first_seen"]),
        is_old=bool(r["is_old"]),
        popularity=r["popularity"],
        word_count=r["word_count"],
        read_at=from_iso(r["read_at"]) if r["read_at"] else None,
        read_progress=float(r["read_progress"]),
        liked=bool(r["liked"]),
        saved=bool(r["saved"]),
        hidden=bool(r["hidden"]),
    )


def list_items(
    conn: sqlite3.Connection,
    f: ItemFilter,
    *,
    now: datetime,
    limit: int = 500,
    pinned: str | None = None,
) -> tuple[list[ItemRow], int]:
    """`pinned` (the item open in the reader) stays listed even if the filter would drop it."""
    where: list[str] = [_SHOW[f.show]]
    args: list[object] = []
    if (floor := date_floor(f.date, now)) is not None:
        where.append("i.first_seen >= ?")
        args.append(to_iso(floor))
    if (query := fts_query(f.search)) is not None:
        where.append("i.pk IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
        args.append(query)
    for column, values in (("i.source_id", f.sources), ("i.kind", f.kinds)):
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            args.extend(sorted(values))
    condition = " AND ".join(where)
    if pinned is not None:
        condition = f"({condition}) OR i.id = ?"
        args.append(pinned)
    sql = f"{_SELECT} WHERE {condition} ORDER BY {_ORDER[f.sort]} LIMIT ?"
    rows = [_row(r) for r in conn.execute(sql, [*args, limit])]
    total = int(conn.execute("SELECT count(*) FROM items").fetchone()[0])
    return rows, total


def get_item_row(conn: sqlite3.Connection, item_id: str, *, now: datetime) -> ItemRow | None:
    row = conn.execute(f"{_SELECT} WHERE i.id = ?", (item_id,)).fetchone()
    return _row(row) if row else None
