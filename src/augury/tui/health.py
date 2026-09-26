import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text

from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.llm.resolver import RoleStatus
from augury.tui.query import count_new


@dataclass(frozen=True)
class HealthSnapshot:
    as_of: datetime
    last_scout_at: datetime | None
    last_scout_status: str | None
    new_items: int  # P11: the same rule as the rows' ✦ marker, so the two always agree
    sources: dict[str, int] = field(default_factory=dict)
    scouting: bool = False
    enrich_error: str | None = None
    new_since: datetime | None = None  # the last visit's start; None on a first launch
    ai: tuple[RoleStatus, ...] = ()


def load_health(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    scouting: bool = False,
    new_since: datetime | None = None,
    ai: tuple[RoleStatus, ...] = (),
) -> HealthSnapshot:
    last = RunsRepo(conn).last("scout", statuses=("ok", "partial", "failed"))
    counts = Counter(r.health for r in SourcesRepo(conn).list_all(enabled_only=True))
    return HealthSnapshot(
        now,
        last.started_at if last else None,
        last.status if last else None,
        new_items=count_new(conn, new_since),
        sources=dict(counts),
        scouting=scouting,
        enrich_error=last.stats.get("enrich_error") if last else None,
        new_since=new_since,
        ai=ai,
    )


def humanize_ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def health_line(s: HealthSnapshot, palette: Mapping[str, str]) -> Text:
    line = Text()
    if s.scouting:
        line.append("Scouting…", style=f"bold {palette.get('accent', '')}")
    elif s.last_scout_at is None:
        line.append("Scout: never", style="dim")
    else:
        failed = s.last_scout_status == "failed"
        ago = humanize_ago(s.last_scout_at, s.as_of)
        label = f"Scout: {s.last_scout_at.astimezone():%H:%M} ({ago})" + (
            " failed" if failed else ""
        )
        line.append(label, style=palette.get("error", "") if failed else "")
        if s.enrich_error:
            line.append(" · enrich ⚠", style=palette.get("warning", ""))
    line.append(f" · {s.new_items} new" + (" since last visit" if s.new_since else ""))
    line.append("  │  Sources: ")
    ok = s.sources.get("ok", 0) + s.sources.get("never", 0)
    line.append(f"{ok} ✓", style=palette.get("success", ""))
    if degraded := s.sources.get("degraded", 0):
        line.append(f" {degraded} ⚠", style=palette.get("warning", ""))
    if broken := s.sources.get("broken", 0):
        line.append(f" {broken} ✗", style=palette.get("error", ""))
    if not any(r.ok for r in s.ai):
        line.append("  │  AI: not configured", style="dim")  # exactly the M1 text
        return line
    line.append("  │  ")
    for i, r in enumerate(s.ai):
        if i:
            line.append("  ")
        line.append(f"{r.role}: {r.provider} ")
        line.append("✓" if r.ok else "✗", style=palette.get("success" if r.ok else "error", ""))
    return line
