"""The daily digest as portable Markdown (spec §8.5): YAML frontmatter and standard relative
links, nothing Obsidian-only, so the folder works in Obsidian, VS Code, GitHub or a static-site
generator. Augury owns <path>/Daily/YYYY-MM-DD.md and rewrites it after each scout."""

import contextlib
import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from augury.core.clock import local_day_bounds, to_iso
from augury.core.config import Config
from augury.core.db.digest_repo import DigestRepo, DigestRow
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import HIDING_FLAGS, Item, Signals, TriageResult
from augury.core.text import strip_control_chars

DAILY_DIR = "Daily"
FRONTMATTER_TYPE = "augury-daily"
# Inline Markdown and HTML, plus what Obsidian also reads inline (#tag, $math$, ^block,
# %%comment%%), plus braces: Liquid (Jekyll) and Hugo shortcodes run before Markdown and ignore
# its escapes, but "\{\{" and "\{\%" hold no "{{" or "{%" for them to find. Fetched text only
# ever follows a line prefix of ours, so block markers can't start a line.
_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]<>|~$#^{}%])")
# What would end a Markdown link destination (space, parentheses, a trailing backslash),
# open raw HTML or a template tag. Whitespace and other invisible characters are encoded too,
# so a URL can't carry a line break out of its link.
_URL_UNSAFE = frozenset(' ()<>{}"\\^`|')
_ARXIV_ID = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")  # adapters pass the API's id through as-is


def md_text(value: str) -> str:
    """Fetched or model text as inert Markdown: one line, metacharacters escaped."""
    value = value.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate becomes "?"
    return _INLINE_SPECIAL.sub(r"\\\1", " ".join(strip_control_chars(value).split()))


def md_url(url: str) -> str | None:
    """Only http(s) links, with anything that would end the link or its line encoded."""
    try:
        scheme = urlsplit(url).scheme  # lower-cased, so "JavaScript:" is refused too
    except ValueError:  # e.g. a malformed IPv6 host
        return None
    if scheme not in ("http", "https"):
        return None
    return "".join(
        quote(c, safe="", errors="replace")
        if c in _URL_UNSAFE or c.isspace() or not c.isprintable()
        else c
        for c in url
    )


def daily_path(root: Path, day: date) -> Path:
    return root / DAILY_DIR / f"{day.isoformat()}.md"


def last_scout_run_id(conn: sqlite3.Connection, day: date) -> str | None:
    start, end = local_day_bounds(day)
    row = conn.execute(
        "SELECT id FROM runs WHERE kind = 'scout' AND status IN ('ok', 'partial')"
        " AND started_at >= ? AND started_at < ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (to_iso(start), to_iso(end)),
    ).fetchone()
    return row[0] if row else None


def _signal_notes(signals: Signals | None) -> list[str]:
    if signals is None:
        return []
    notes: list[str] = []
    if signals.upvotes is not None:
        notes.append(f"{signals.upvotes} upvotes")
    elif signals.upvotes7d is not None:
        notes.append(f"{signals.upvotes7d} upvotes this week")
    if signals.github_stars:
        notes.append(f"{signals.github_stars} GitHub stars")
    if signals.comments:
        notes.append(f"{signals.comments} comments")
    return notes


def _entry(
    n: int,
    row: DigestRow,
    item: Item,
    result: TriageResult | None,
    source: str,
    signals: Signals | None,
) -> list[str]:
    lines = [f"## {n}. {md_text(item.title)}", ""]
    if result is not None and result.why_read:
        lines += [f"**Why read:** {md_text(result.why_read)}", ""]
    meta = [source, item.kind, f"score {row.final_score * 10:.1f}", *_signal_notes(signals)]
    lines.append("- " + " · ".join(md_text(m) for m in meta))
    if result is not None and result.tags:
        lines.append("- Tags: " + ", ".join(md_text(tag) for tag in result.tags))
    links: list[str] = []
    if (url := md_url(item.url)) is not None:
        links.append(f"[Open]({url})")
    if item.arxiv_id and _ARXIV_ID.fullmatch(item.arxiv_id):
        links.append(f"[arXiv](https://arxiv.org/abs/{item.arxiv_id})")
    if links:
        lines.append("- Links: " + " · ".join(links))
    return [*lines, ""]


def render_daily(
    conn: sqlite3.Connection, day: date, *, run_id: str | None, root: Path | None = None
) -> str:
    items, triage = ItemsRepo(conn), TriageRepo(conn)
    names = {r.source.id: r.source.name for r in SourcesRepo(conn).list_all()}
    rows = DigestRepo(conn).for_day(day)
    signals = items.latest_signals([r.item_id for r in rows])
    shown: list[tuple[DigestRow, Item, TriageResult | None]] = []
    hidden: list[tuple[Item, TriageResult]] = []
    for row in rows:
        if (item := items.get(row.item_id)) is None:
            continue
        result = triage.get(row.item_id)
        if result is not None and HIDING_FLAGS & set(result.flags):
            hidden.append((item, result))
        else:
            shown.append((row, item, result))
    lines = [
        "---",
        f"type: {FRONTMATTER_TYPE}",
        f"date: {day.isoformat()}",
        f"run_id: {run_id or 'none'}",
        "---",
        "",
        f"# Augury · {day:%A %d %B %Y}",
        "",
    ]
    before = day - timedelta(days=1)
    if root is not None and daily_path(root, before).exists():  # a standard relative link
        lines += [f"Previous: [{before.isoformat()}]({before.isoformat()}.md)", ""]
    if not rows:
        lines += ["No digest for this day yet: run `augury scout`.", ""]
    for n, (row, item, result) in enumerate(shown, 1):
        source = names.get(item.source_id, item.source_id)
        lines += _entry(n, row, item, result, source, signals.get(item.id))
    if hidden:
        lines += [f"## Hidden by triage ({len(hidden)})", ""]
        for item, result in hidden:
            flags = ", ".join(sorted(HIDING_FLAGS & set(result.flags)))
            url = md_url(item.url)
            lines.append(
                f"- {md_text(item.title)} ({flags})" + (f" · [Open]({url})" if url else "")
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_daily(
    conn: sqlite3.Connection, config: Config, day: date, *, run_id: str | None
) -> Path | None:
    """Write <path>/Daily/<day>.md. None when [export] path is empty: export is off."""
    if not config.export.path.strip():
        return None
    root = Path(config.export.path).expanduser()
    target = daily_path(root, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_daily(conn, day, run_id=run_id, root=root)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)  # a reader (or a vault sync) never sees a half-written file
    except BaseException:
        with contextlib.suppress(OSError):  # keep the original error, not the cleanup's
            tmp.unlink(missing_ok=True)
        raise
    return target
