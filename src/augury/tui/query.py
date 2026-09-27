import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from augury.core.clock import from_iso, local_day, local_day_bounds, to_iso
from augury.core.models import HIDING_FLAGS

DateRange = Literal["today", "7d", "30d", "all"]
SortKey = Literal["score", "newest", "popular", "reading_time"]
ShowKey = Literal["unread", "new", "all", "saved", "liked", "hidden"]
DATE_RANGES: tuple[DateRange, ...] = ("today", "7d", "30d", "all")
SORT_KEYS: tuple[SortKey, ...] = ("score", "newest", "popular", "reading_time")
SHOW_KEYS: tuple[ShowKey, ...] = ("unread", "new", "all", "saved", "liked", "hidden")
TOP_VIEWS: tuple[ShowKey, ...] = ("unread", "new", "all")  # views that leave promo/thin out
WORDS_PER_MINUTE = 230


@dataclass(frozen=True)
class ItemFilter:
    search: str = ""
    sources: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    date: DateRange = "today"
    sort: SortKey = "score"  # unranked items fall back to newest (see _ORDER)
    show: ShowKey = "unread"
    show_triage_hidden: bool = False  # H: include the items triage flagged promo or thin


DIGEST_PRESET = ItemFilter()  # spec §8.3.3: Today · Unread · Score


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
    score: float | None = None  # digests.final_score in [0, 1]; shown out of 10
    why_read: str = ""
    tags: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()
    breakdown_json: str | None = None
    # The user has ★ liked (and not hidden) an item from this source: "source you like" in
    # the ranking's explanation needs that, not just a high smoothed like rate (Task 26 review).
    source_liked: bool = False

    @property
    def reading_minutes(self) -> int | None:
        return max(1, round(self.word_count / WORDS_PER_MINUTE)) if self.word_count else None

    @property
    def read_state(self) -> Literal["unread", "partial", "read"]:
        if self.read_at is None:
            return "unread"
        return "read" if self.read_progress >= 0.9 else "partial"


@dataclass(frozen=True)
class TriageHidden:
    """What a view leaves out because triage flagged it promo or thin: how many, per flag."""

    total: int = 0
    by_flag: Mapping[str, int] = field(default_factory=dict)


_COLUMNS = """
SELECT i.id, i.title, i.source_id, i.kind, i.url, i.published_at, i.first_seen, i.is_old,
       COALESCE(sig.upvotes7d, sig.upvotes) AS popularity, c.word_count,
       st.read_at, COALESCE(st.read_progress, 0) AS read_progress,
       COALESCE(st.liked, 0) AS liked, COALESCE(st.saved, 0) AS saved,
       COALESCE(st.hidden, 0) AS hidden,
       d.final_score AS score, d.breakdown_json, t.why_read, t.tags_json, t.flags_json
"""
# One digest row per item: (day, item_id) is the key, and only the latest day is joined.
_FROM = """
FROM items i
LEFT JOIN item_state st ON st.item_id = i.id
LEFT JOIN contents c ON c.item_id = i.id AND c.status = 'ok'
LEFT JOIN signals sig ON sig.item_id = i.id
     AND sig.observed_on = (SELECT MAX(s2.observed_on) FROM signals s2 WHERE s2.item_id = i.id)
LEFT JOIN digests d ON d.item_id = i.id
     AND d.day = (SELECT MAX(d2.day) FROM digests d2 WHERE d2.item_id = i.id)
LEFT JOIN triage t ON t.item_id = i.id
"""
_SELECT = _COLUMNS + _FROM
_SHOW = {
    "unread": "st.read_at IS NULL AND COALESCE(st.hidden, 0) = 0",
    "new": "st.read_at IS NULL AND COALESCE(st.hidden, 0) = 0",  # + new_clause()'s date
    "all": "COALESCE(st.hidden, 0) = 0",
    "saved": "st.saved = 1 AND COALESCE(st.hidden, 0) = 0",
    "liked": "st.liked = 1 AND COALESCE(st.hidden, 0) = 0",
    "hidden": "st.hidden = 1",
}
_NEWEST = "i.first_seen DESC, COALESCE(i.published_at, i.first_seen) DESC, i.pk DESC"
_ORDER = {
    "score": f"score IS NULL, score DESC, {_NEWEST}",  # unranked items fall back to newest
    "newest": _NEWEST,
    "popular": "popularity IS NULL, popularity DESC, i.first_seen DESC",
    "reading_time": "c.word_count IS NULL, c.word_count ASC, i.first_seen DESC",
}
_HIDING = ", ".join(f"'{flag}'" for flag in sorted(HIDING_FLAGS))  # our constants, not input
_TRIAGE_HIDDEN = (
    f"EXISTS (SELECT 1 FROM json_each(COALESCE(t.flags_json, '[]')) WHERE value IN ({_HIDING}))"
)


def new_clause(since: datetime | None) -> tuple[str, list[object]]:
    """P11's one rule for "new since your last visit", in SQL (`is_new` is the same in Python):
    unread, not hidden, and first seen after the last visit -- or ever, on a first launch."""
    if since is None:
        return _SHOW["new"], []
    return f"{_SHOW['new']} AND i.first_seen > ?", [to_iso(since)]


def is_new(row: ItemRow, since: datetime | None) -> bool:
    """`new_clause` for one row. first_seen is stored to the second, and to_iso() truncates
    `since` the same way, so both sides agree even within the visit's first second."""
    unseen = since is None or row.first_seen > since
    return row.read_at is None and not row.hidden and unseen


def count_new(conn: sqlite3.Connection, since: datetime | None) -> int:
    clause, args = new_clause(since)
    sql = (
        f"SELECT count(*) FROM items i LEFT JOIN item_state st ON st.item_id = i.id WHERE {clause}"
    )
    return int(conn.execute(sql, args).fetchone()[0])


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


def _row(r: sqlite3.Row, liked_sources: frozenset[str]) -> ItemRow:
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
        score=r["score"],
        why_read=r["why_read"] or "",
        tags=tuple(json.loads(r["tags_json"])) if r["tags_json"] else (),
        flags=frozenset(json.loads(r["flags_json"])) if r["flags_json"] else frozenset(),
        breakdown_json=r["breakdown_json"],
        source_liked=r["source_id"] in liked_sources,
    )


def _liked_sources(conn: sqlite3.Connection) -> frozenset[str]:
    """The sources with a ★ like; a hidden like is no like (as agents/affinity.py counts)."""
    rows = conn.execute(
        "SELECT DISTINCT i.source_id FROM items i JOIN item_state st ON st.item_id = i.id"
        " WHERE st.liked = 1 AND st.hidden = 0"
    )
    return frozenset(r[0] for r in rows)


def _conditions(
    f: ItemFilter,
    now: datetime,
    new_since: datetime | None,
    ranked: Sequence[str] | None = None,
) -> tuple[list[str], list[object]]:
    """What a view contains, as WHERE clauses over _FROM. The list, the hidden-by-triage count
    and the tag options all use it, so they always agree about which items that is."""
    where: list[str] = [_SHOW[f.show]]
    args: list[object] = []
    if f.show == "new":  # the last visit is its own floor: the Date chip doesn't apply
        clause, args = new_clause(new_since)
        where = [clause]
    elif (floor := date_floor(f.date, now)) is not None:
        where.append("i.first_seen >= ?")
        args.append(to_iso(floor))
    if ranked is not None:  # M4: Enter's semantic results stand in for the typed FTS match
        where.append("i.id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(list(ranked)))
    elif (query := fts_query(f.search)) is not None:
        where.append("i.pk IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
        args.append(query)
    for column, values in (("i.source_id", f.sources), ("i.kind", f.kinds)):
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            args.extend(sorted(values))
    if f.tags:
        marks = ",".join("?" * len(f.tags))
        where.append(
            "EXISTS (SELECT 1 FROM json_each(COALESCE(t.tags_json, '[]'))"
            f" WHERE value IN ({marks}))"
        )
        args.extend(sorted(f.tags))
    if f.show in TOP_VIEWS and not f.show_triage_hidden:
        where.append(f"NOT {_TRIAGE_HIDDEN}")
    return where, args


def list_items(
    conn: sqlite3.Connection,
    f: ItemFilter,
    *,
    now: datetime,
    limit: int = 500,
    pinned: str | None = None,
    new_since: datetime | None = None,
    ranked: Sequence[str] | None = None,
) -> tuple[list[ItemRow], int]:
    """`pinned` (the item open in the reader) stays listed even if the filter would drop it.
    `new_since` is the last visit's start, for Show: New (None on a first launch). `ranked`
    (M4): semantic search's items, best first; they replace the search match, keep the view's
    other conditions, and are listed in that order instead of the Sort chip's."""
    where, args = _conditions(f, now, new_since, ranked)
    condition = " AND ".join(where)
    if pinned is not None:
        condition = f"({condition}) OR i.id = ?"
        args.append(pinned)
    order = _ORDER[f.sort]
    if ranked is not None:
        # Where the item ranked; the pinned item (the one being read) may be absent: last.
        position = "(SELECT key FROM json_each(?) WHERE value = i.id)"
        order = f"{position} IS NULL, {position}, {_NEWEST}"
        args.extend([json.dumps(list(ranked))] * 2)
    sql = f"{_SELECT} WHERE {condition} ORDER BY {order} LIMIT ?"
    liked = _liked_sources(conn)
    rows = [_row(r, liked) for r in conn.execute(sql, [*args, limit])]
    total = int(conn.execute("SELECT count(*) FROM items").fetchone()[0])
    return rows, total


def get_item_row(conn: sqlite3.Connection, item_id: str, *, now: datetime) -> ItemRow | None:
    row = conn.execute(f"{_SELECT} WHERE i.id = ?", (item_id,)).fetchone()
    return _row(row, _liked_sources(conn)) if row else None


def triage_hidden(
    conn: sqlite3.Connection, f: ItemFilter, *, now: datetime, new_since: datetime | None = None
) -> TriageHidden:
    """The items this view leaves out because triage flagged them promo or thin (§5.4)."""
    if f.show not in TOP_VIEWS:
        return TriageHidden()
    where, args = _conditions(replace(f, show_triage_hidden=True), now, new_since)
    where.append(_TRIAGE_HIDDEN)
    rows = conn.execute(f"SELECT t.flags_json {_FROM} WHERE {' AND '.join(where)}", args)
    counts: Counter[str] = Counter()
    total = 0
    for r in rows:
        total += 1
        counts.update(flag for flag in set(json.loads(r[0])) if flag in HIDING_FLAGS)
    return TriageHidden(total, dict(sorted(counts.items())))


def tag_options(
    conn: sqlite3.Connection, f: ItemFilter, *, now: datetime, new_since: datetime | None = None
) -> list[tuple[str, int]]:
    """The tags on the items this view shows (ignoring its own tag filter), most common first."""
    where, args = _conditions(replace(f, tags=frozenset()), now, new_since)
    sql = (
        f"SELECT tag.value AS tag, count(DISTINCT i.id) AS n {_FROM}"
        " JOIN json_each(COALESCE(t.tags_json, '[]')) AS tag"
        f" WHERE {' AND '.join(where)} AND tag.type = 'text'"
        " GROUP BY tag.value ORDER BY n DESC, tag.value"
    )
    return [(str(r["tag"]), int(r["n"])) for r in conn.execute(sql, args)]
