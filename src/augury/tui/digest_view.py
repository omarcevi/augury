"""How digest data looks in the TUI: score cells, the breakdown line, the hidden-by-triage row
and the degraded banner. The str helpers return plain text; callers wrap it with safe_text."""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from rich.text import Text

from augury.core.db.runs_repo import RunsRepo
from augury.tui.query import ItemRow, TriageHidden

TERM_LABELS = {"rel": "relevance", "pop": "popularity", "rec": "recency", "nov": "novelty"}
LIKED_SOURCE = 0.5  # likes mode: the source term from which "source you like" can be said


def score_text(score: float | None, palette: Mapping[str, str]) -> Text:
    """Out of 10: green from 8, yellow from 6, muted below (spec §8.3.5)."""
    if score is None:
        return Text("—", justify="right")
    shown = round(score * 10, 1)
    if shown >= 8:
        style = palette.get("success", "")
    elif shown >= 6:
        style = palette.get("warning", "")
    else:
        style = "dim"
    return Text(f"{shown:.1f}", style=style, justify="right")


def breakdown_line(breakdown_json: str | None, *, source_liked: bool = False) -> str:
    """'score 9.2 = relevance 0.90×0.77 + recency 1.00×0.23' (term × its weight), or in likes
    mode 'score 7.0 · matches your likes: diffusion · source you like · 2h ago'. `source_liked`:
    the user has ★ liked something from the item's source (ItemRow.source_liked)."""  # noqa: RUF002
    if not breakdown_json:
        return ""
    try:
        data = json.loads(breakdown_json)
        if data.get("mode") == "likes":  # pre-flight, user decision: no key, ranked by your ★
            reasons = _likes_reasons(data, source_liked=source_liked)
            return " · ".join([f"score {data['final'] * 10:.1f}", *reasons])
        parts = [
            f"{TERM_LABELS.get(term, term)} {data['terms'][term]:.2f}×{weight:.2f}"  # noqa: RUF001
            for term, weight in data["weights"].items()
        ]
        return f"score {data['final'] * 10:.1f} = " + " + ".join(parts)
    except ValueError, KeyError, TypeError, AttributeError:
        return ""


def _ago(hours: int | None) -> list[str]:
    if hours is None:
        return []
    return ["just now" if hours < 1 else f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"]


def _likes_reasons(data: dict[str, Any], *, source_liked: bool) -> list[str]:
    """'matches your likes: diffusion, rl · source you like · 2h ago' (spec'd by the user)."""
    reasons: list[str] = []
    if matched := data.get("matched"):
        reasons.append("matches your likes: " + ", ".join(str(t) for t in matched))
    # A high source term alone isn't enough: the smoothed like rate (agents/affinity.py) rates
    # a source nobody has opened above one with a single like among several reads.
    if source_liked and data["terms"].get("source", 0) >= LIKED_SOURCE:
        reasons.append("source you like")
    return reasons + _ago(data.get("age_hours"))


def hidden_line(hidden: TriageHidden, *, expanded: bool) -> str:
    if expanded:
        return f"── showing {hidden.total} hidden by triage · H hides them again ──"
    counts = " · ".join(f"{flag} ×{n}" for flag, n in sorted(hidden.by_flag.items()))  # noqa: RUF001
    return f"── hidden by triage ({hidden.total}): {counts} · H shows them ──"


def degraded_banner(conn: sqlite3.Connection) -> str:
    """'ranking degraded: <reason>' when the last scout ranked without triage (spec §5.3)."""
    last = RunsRepo(conn).last("scout", statuses=("ok", "partial"))
    triage = last.stats.get("triage") if last else None
    if not isinstance(triage, dict) or not triage.get("degraded"):
        return ""
    no_key = triage.get("degraded_kind") == "not_configured"
    # Pre-flight, user decision 2026-09-26: without a key the digest follows your likes.
    hint = " — ranked by your likes (l) and recency · `augury init` adds a key" if no_key else ""
    return f"ranking degraded: {triage['degraded']}{hint}"


def status_selection(row: ItemRow) -> str:
    score = f"  {row.score * 10:.1f}" if row.score is not None else ""
    return f"▶ {row.title}  {row.source_id}{score}"
