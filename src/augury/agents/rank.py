"""Ranking (spec §5.4), pure code. With a model key (`ai` mode):

    final = Σ wᵢ·termᵢ over the terms an item has, weights renormalized to sum to 1
    rel = relevance / 10
    pop = percentile of the item's popularity within its source over the last 30 days
    rec = exp(-age_days / 3)
    nov = 1 - the highest similarity to what you read in the last 60 days (M4, rag/cluster.py)

A missing term hands its weight to the item's other terms, so a blog without upvotes is never
penalized for having none.

Without a key (user decision 2026-09-26), `likes` mode ranks on how much an item looks like
what you ★ liked (`topic`, `source`, see agents/affinity.py) plus recency; before the first
like, `cold` mode has only popularity and recency (the spec's missing-signal rule)."""

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from augury.agents.affinity import like_affinity
from augury.core.clock import from_iso, local_day_bounds, to_iso
from augury.core.config import RankingConfig
from augury.core.db.digest_repo import DigestRepo
from augury.core.models import HIDING_FLAGS
from augury.rag.cluster import novelty_scores

Mode = Literal["ai", "likes", "cold"]  # pre-flight, user decision 2026-09-26
# The signal that counts as popularity, per built-in recipe type; other sources have none.
POPULARITY_SIGNAL = {"hf_papers": "upvotes", "hf_blog": "upvotes7d", "hf_community": "upvotes7d"}
POPULARITY_WINDOW = timedelta(days=30)
RECENCY_DAYS = 3.0


def ai_weights(c: RankingConfig) -> dict[str, float]:  # spec §5.4
    return {"rel": c.w_rel, "pop": c.w_pop, "rec": c.w_rec, "nov": c.w_nov}


def like_weights(c: RankingConfig) -> dict[str, float]:  # no key, some likes
    return {"topic": c.w_topic, "source": c.w_source, "rec": c.w_fresh}


def cold_weights(c: RankingConfig) -> dict[str, float]:  # no key, no likes: §5.4's missing rule
    return {"pop": c.w_pop, "rec": c.w_rec}


_WEIGHTS = {"ai": ai_weights, "likes": like_weights, "cold": cold_weights}


@dataclass(frozen=True)
class Breakdown:
    final: float
    terms: dict[str, float]  # the terms this item has, each in [0, 1]
    weights: dict[str, float]  # after redistribution; they sum to 1
    missing: list[str]
    mode: Mode = "ai"
    matched: tuple[str, ...] = ()  # likes mode: the liked terms this item matches
    age_hours: int | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "final": round(self.final, 4),
                "mode": self.mode,
                "terms": {k: round(v, 4) for k, v in self.terms.items()},
                "weights": {k: round(v, 4) for k, v in self.weights.items()},
                "missing": self.missing,
                "matched": list(self.matched),
                "age_hours": self.age_hours,
            }
        )


class DigestStats(BaseModel):
    day: str
    mode: Mode = "ai"
    items: int = 0
    hidden: int = 0  # flagged promo or thin: ranked, but left out of the Top view
    without_relevance: int = 0


@dataclass(frozen=True)
class Ranked:
    item_id: str
    breakdown: Breakdown
    hidden: bool


def recency(age_days: float) -> float:
    return math.exp(-max(0.0, age_days) / RECENCY_DAYS)


def percentile(value: float, population: Sequence[float]) -> float:
    """Mid-rank percentile: the share strictly below plus half the ties (the item included)."""
    if not population:
        return 0.5
    below = sum(1 for p in population if p < value)
    ties = sum(1 for p in population if p == value)
    return (below + 0.5 * ties) / len(population)


def combine(terms: Mapping[str, float | None], weights: Mapping[str, float]) -> Breakdown:
    """Only the terms named in `weights` count; the rest of `terms` is ignored."""
    available = {k: v for k in weights if (v := terms.get(k)) is not None}
    missing = [k for k in weights if k not in available]
    total = sum(weights[k] for k in available)
    if total <= 0:  # every term this item has is weighted 0: nothing to rank on
        return Breakdown(0.0, available, {}, missing)
    effective = {k: weights[k] / total for k in available}
    final = sum(effective[k] * available[k] for k in available)
    return Breakdown(final, available, effective, missing)


def _popularity(
    conn: sqlite3.Connection, now: datetime
) -> tuple[dict[str, float], dict[str, list[float]]]:
    """Each item's popularity, and each source's population of them over the last 30 days."""
    rows = conn.execute(
        "SELECT i.id, i.source_id, s.recipe_type, sig.upvotes, sig.upvotes7d FROM items i"
        " JOIN sources s ON s.id = i.source_id"
        " JOIN signals sig ON sig.item_id = i.id AND sig.observed_on ="
        " (SELECT MAX(s2.observed_on) FROM signals s2 WHERE s2.item_id = i.id)"
        " WHERE i.first_seen >= ?",
        (to_iso(now - POPULARITY_WINDOW),),
    )
    by_item: dict[str, float] = {}
    population: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        signal = POPULARITY_SIGNAL.get(r["recipe_type"])
        if signal is None or r[signal] is None:
            continue
        by_item[r["id"]] = float(r[signal])
        population[r["source_id"]].append(float(r[signal]))
    return by_item, population


def rank_day(
    conn: sqlite3.Connection, day: date, *, now: datetime, config: RankingConfig, ai: bool
) -> tuple[Mode, list[Ranked]]:
    """`ai`: a model key is set (spec formula). Otherwise your likes, or (none yet) pop + rec."""
    start, end = local_day_bounds(day)
    rows = conn.execute(
        "SELECT i.id, i.source_id, i.first_seen, i.published_at, t.relevance, t.flags_json"
        " FROM items i LEFT JOIN triage t ON t.item_id = i.id"
        " WHERE i.first_seen >= ? AND i.first_seen < ?"
        " AND i.id NOT IN (SELECT item_id FROM digests WHERE day < ?)",  # shown once (§5.4)
        (to_iso(start), to_iso(end), day.isoformat()),
    ).fetchall()
    pop_by_item, population = _popularity(conn, now)
    affinity = (
        None if ai else like_affinity(conn, [r["id"] for r in rows], max_terms=config.like_terms)
    )
    mode: Mode = "ai" if ai else "likes" if affinity is not None else "cold"
    weights = _WEIGHTS[mode](config)
    # M4: only the spec formula weighs novelty; without vectors or reads it is unavailable.
    novelty = novelty_scores(conn, [r["id"] for r in rows], today=day) if ai else {}
    scored: list[tuple[Ranked, datetime]] = []
    for r in rows:
        seen = from_iso(r["first_seen"])
        published = from_iso(r["published_at"]) if r["published_at"] else None
        age_days = (now - (published or seen)).total_seconds() / 86400
        pop = pop_by_item.get(r["id"])
        terms: dict[str, float | None] = {
            "rel": r["relevance"] / 10 if r["relevance"] is not None else None,
            "pop": percentile(pop, population[r["source_id"]]) if pop is not None else None,
            "rec": recency(age_days),
            "nov": novelty.get(r["id"]),
        }
        matched: tuple[str, ...] = ()
        if affinity is not None:
            # No useful liked terms (all stopwords, say): topic is missing, not 0.
            terms["topic"] = affinity.topic.get(r["id"], 0.0) if affinity.terms else None
            terms["source"] = affinity.source.get(r["source_id"], 0.0)
            matched = tuple(affinity.matched.get(r["id"], []))
        b = combine(terms, weights)
        b = replace(b, mode=mode, matched=matched, age_hours=max(0, round(age_days * 24)))
        flags = set(json.loads(r["flags_json"])) if r["flags_json"] else set()
        scored.append((Ranked(r["id"], b, bool(flags & HIDING_FLAGS)), seen))
    # Ties: the newer item first, then a stable id order.
    scored.sort(key=lambda pair: (-pair[0].breakdown.final, -pair[1].timestamp(), pair[0].item_id))
    return mode, [ranked for ranked, _seen in scored]


def build_digest(
    conn: sqlite3.Connection, day: date, *, now: datetime, config: RankingConfig, ai: bool
) -> DigestStats:
    mode, ranked = rank_day(conn, day, now=now, config=config, ai=ai)
    DigestRepo(conn).replace_day(
        day,
        [(r.item_id, n, r.breakdown.final, r.breakdown.to_json()) for n, r in enumerate(ranked, 1)],
    )
    return DigestStats(
        day=day.isoformat(),
        mode=mode,
        items=len(ranked),
        hidden=sum(1 for r in ranked if r.hidden),
        without_relevance=sum(1 for r in ranked if "rel" not in r.breakdown.terms),
    )
