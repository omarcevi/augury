"""How much an item looks like what the user ★ liked: a topic score (FTS5 bm25 against the top
terms of liked titles and summaries) and a per-source like rate. Hidden items never count;
saved items aren't a signal (yet). User decision 2026-09-26: without a model key the digest
ranks on these plus recency."""

import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched

# The token characters of items_fts (unicode61 with tokenchars '-._'), as tui.query.fts_query.
_TOKEN = re.compile(r"[\w.\-]+")
# English function words plus the filler of blog prose ("actually", "basically"): liked
# summaries are full of them, and they'd crowd out the topics. Split contractions leave
# "don", "ll", "ve" and the like behind.
_STOPWORD_LIST = """
a about above across actually after again against ago all almost along already also although
always am among an and another any anyone anything apparently are around as at away back
basically be became because become been before behind being below best better between big
bit both but by can could did didn do does doesn doing don done down during each easy either
else enough especially etc even ever every example few first for found from full further get
gets getting give go goes going good got great had has have having he her here hers him his
how however if in instead into is isn it its itself just keep know last later least less let
lets like likely ll lot lots made make makes many may me might more most much must my near
need needs never new next no nor not now of off often on once one only onto or other others
our ours out over own part per pretty quite rather re real really right same see seems several
she should show simply since so some something still such sure take than that the their them
themselves then there these they thing things think this those though through thus time to
today too two under until up upon us use used uses using ve very via vs want was way ways we
well were what whatever when where whether which while who whom whose why will with within
without won work works would yet you your yours
"""
STOPWORDS = frozenset(_STOPWORD_LIST.split())
SOURCE_ALPHA, SOURCE_BETA = 1.0, 10.0  # (likes + alpha) / (seen + beta)
MATCHED_SHOWN = 3
_BATCH = 500  # ids per IN (...) list, well under SQLite's variable limit


@dataclass(frozen=True)
class LikeAffinity:
    terms: list[str]
    topic: dict[str, float]  # item id → 0..1 (the best candidate is 1; no match is 0)
    source: dict[str, float]  # source id → 0..1 (the most-liked source is 1)
    matched: dict[str, list[str]]  # item id → the liked terms it matches, best first


def _tokens(text: str) -> set[str]:
    return {t.strip("._-").lower() for t in _TOKEN.findall(text)} - {""}


def _useful(token: str) -> bool:
    # At least one letter: numbers, versions and dates ("2026-09-25", "0.5") aren't topics.
    return 2 <= len(token) <= 40 and token not in STOPWORDS and any(c.isalpha() for c in token)


def liked_terms(conn: sqlite3.Connection, limit: int) -> list[str]:
    """The most common terms across liked, non-hidden items (each item counts a term once).
    Ties go to terms from liked titles: with only a few likes most terms are tied at 1."""
    counts: Counter[str] = Counter()
    in_titles: Counter[str] = Counter()
    rows = conn.execute(
        "SELECT i.title, i.summary FROM items i JOIN item_state st ON st.item_id = i.id"
        " WHERE st.liked = 1 AND st.hidden = 0"
    )
    for r in rows:
        title = {t for t in _tokens(r["title"]) if _useful(t)}
        counts.update(title | {t for t in _tokens(r["summary"]) if _useful(t)})
        in_titles.update(title)
    ranked = sorted(counts, key=lambda t: (-counts[t], -in_titles[t], t))
    return ranked[:limit]


def terms_query(terms: Sequence[str]) -> str | None:
    """`"t1"* OR "t2"* …`: every term quoted (as tui.query.fts_query does), so no FTS syntax."""
    cleaned = (t.replace('"', "") for t in terms)
    return " OR ".join(f'"{t}"*' for t in cleaned if t) or None


def _topic(conn: sqlite3.Connection, item_ids: Sequence[str], query: str) -> dict[str, float]:
    raw: dict[str, float] = {}
    for chunk in batched(item_ids, _BATCH, strict=False):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT i.id, bm25(items_fts) AS score FROM items_fts"
            " JOIN items i ON i.pk = items_fts.rowid"
            f" WHERE items_fts MATCH ? AND i.id IN ({marks})",
            [query, *chunk],
        )
        raw.update((r["id"], -float(r["score"])) for r in rows)  # bm25: lower is better
    best = max(raw.values(), default=0.0)
    return {i: (raw.get(i, 0.0) / best if best > 0 else 0.0) for i in item_ids}


def _source(conn: sqlite3.Connection) -> dict[str, float]:
    """Smoothed like rate per source, (likes + alpha) / (seen + beta), scaled so the best
    is 1. Seen means opened or liked; a hidden like is no like."""
    rows = conn.execute(
        "SELECT i.source_id,"
        " SUM(COALESCE(st.liked, 0) = 1 AND COALESCE(st.hidden, 0) = 0) AS likes,"
        " SUM(st.read_at IS NOT NULL OR COALESCE(st.liked, 0) = 1) AS seen"
        " FROM items i LEFT JOIN item_state st ON st.item_id = i.id GROUP BY i.source_id"
    )
    rates = {r["source_id"]: (r["likes"] + SOURCE_ALPHA) / (r["seen"] + SOURCE_BETA) for r in rows}
    best = max(rates.values(), default=0.0)
    return {s: rate / best for s, rate in rates.items()} if best > 0 else {}


def _matched(
    conn: sqlite3.Connection, item_ids: Sequence[str], terms: Sequence[str]
) -> dict[str, list[str]]:
    """The liked terms each item contains, prefix-matched like the `"t"*` query. A term that
    overlaps one already listed ("agent" after "agents") would only repeat it."""
    matched: dict[str, list[str]] = {}
    for chunk in batched(item_ids, _BATCH, strict=False):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT id, title, summary FROM items WHERE id IN ({marks})", chunk):
            tokens = _tokens(f"{r['title']} {r['summary']}")
            hits: list[str] = []
            for t in terms:
                if any(h.startswith(t) or t.startswith(h) for h in hits):
                    continue
                if any(tok.startswith(t) for tok in tokens):
                    hits.append(t)
            matched[r["id"]] = hits[:MATCHED_SHOWN]
    return matched


def like_affinity(
    conn: sqlite3.Connection, item_ids: Sequence[str], *, max_terms: int = 30
) -> LikeAffinity | None:
    """None when the user has liked nothing (cold start: the caller ranks without affinity)."""
    liked = conn.execute("SELECT count(*) FROM item_state WHERE liked = 1 AND hidden = 0")
    if not liked.fetchone()[0]:
        return None
    terms = liked_terms(conn, max_terms)
    query = terms_query(terms)
    topic = _topic(conn, item_ids, query) if query and item_ids else dict.fromkeys(item_ids, 0.0)
    # Only explain a topic score that exists: FTS and the Python tokens can disagree at the
    # edges (a leading "_", diacritics), and "matches your likes" must never be shown for 0.
    matched = {
        i: hits if topic.get(i, 0.0) > 0 else []
        for i, hits in _matched(conn, item_ids, terms).items()
    }
    return LikeAffinity(terms, topic, _source(conn), matched)
