"""`augury eval retrieval` (spec §6.6). Labeled pairs come from the database: each blog post
that names an arXiv id is paired with that paper. The query is the blog's title and summary;
the target is the paper's archive passage. recall@1/5/10 and MRR are reported for FTS-only,
vector-only and hybrid search, over the archive collection, the blog itself left out."""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from augury.llm.embedder import Embedder, EmbedMeter
from augury.rag.chunker import CHUNKER_VERSION
from augury.rag.search import FTS_K, RRF_K, VEC_K, Mode, SearchFilters, item_order, search

MIN_PAIRS = 30
KS = (1, 5, 10)
MODES: tuple[Mode, ...] = ("fts", "vector", "hybrid")


class NotEnoughPairs(Exception):
    def __init__(self, found: int) -> None:
        super().__init__(
            f"only {found} blog↔paper pairs in the database; the eval needs {MIN_PAIRS}"
            " (blog posts that name an arXiv paper augury also has)"
        )
        self.found = found


@dataclass(frozen=True)
class Pair:
    blog_id: str
    paper_id: str
    query: str


class ModeScores(BaseModel):
    recall: dict[int, float]  # k -> share of pairs whose paper ranked in the top k
    mrr: float


class EvalReport(BaseModel):
    pairs: int
    embed_model: str
    dimensions: int
    config_hash: str
    created_at: datetime
    modes: dict[str, ModeScores]


def build_pairs(conn: sqlite3.Connection) -> list[Pair]:
    rows = conn.execute(
        "SELECT b.id AS blog, p.id AS paper, b.title, b.summary FROM items b"
        " JOIN items p ON p.arxiv_id = b.arxiv_id AND p.kind = 'paper'"
        " JOIN chunks c ON c.item_id = p.id AND c.collection = 'archive'"
        " WHERE b.kind = 'article' AND b.arxiv_id IS NOT NULL ORDER BY b.id"
    )
    return [Pair(r["blog"], r["paper"], f"{r['title']}\n{r['summary']}".strip()) for r in rows]


def config_hash(embedder: Embedder) -> str:
    settings = {
        "model": embedder.spec,
        "dims": embedder.dimensions,
        "chunker": CHUNKER_VERSION,
        "rrf_k": RRF_K,
        "fts_k": FTS_K,
        "vec_k": VEC_K,
    }
    return hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:8]


def score(ranks: Sequence[int | None]) -> ModeScores:
    n = len(ranks) or 1
    recall = {k: sum(1 for r in ranks if r is not None and r <= k) / n for k in KS}
    mrr = sum(1 / r for r in ranks if r is not None) / n
    return ModeScores(recall=recall, mrr=mrr)


async def run_eval(
    conn: sqlite3.Connection,
    *,
    embedder: Embedder,
    meter: EmbedMeter,
    now: datetime,
    pairs: Sequence[Pair] | None = None,
) -> EvalReport:
    pairs = build_pairs(conn) if pairs is None else list(pairs)
    if len(pairs) < MIN_PAIRS:
        raise NotEnoughPairs(len(pairs))
    modes: dict[str, ModeScores] = {}
    for mode in MODES:
        ranks: list[int | None] = []
        for pair in pairs:
            filters = SearchFilters(
                collections=("archive",), exclude_items=frozenset({pair.blog_id})
            )
            hits = await search(
                conn,
                pair.query,
                filters,
                embedder=embedder,
                meter=meter,
                top_k=max(KS),
                mode=mode,
            )
            order = item_order(hits)
            ranks.append(order.index(pair.paper_id) + 1 if pair.paper_id in order else None)
        modes[mode] = score(ranks)
    return EvalReport(
        pairs=len(pairs),
        embed_model=embedder.spec,
        dimensions=embedder.dimensions,
        config_hash=config_hash(embedder),
        created_at=now,
        modes=modes,
    )


def format_table(report: EvalReport) -> str:
    head = f"{'mode':<8}" + "".join(f"{f'recall@{k}':>11}" for k in KS) + f"{'MRR':>8}"
    lines = [
        f"{report.pairs} blog↔paper pairs · {report.embed_model} ({report.dimensions} dims)",
        head,
    ]
    for mode, s in report.modes.items():
        cells = "".join(f"{s.recall[k]:>11.3f}" for k in KS)
        lines.append(f"{mode:<8}{cells}{s.mrr:>8.3f}")
    return "\n".join(lines)


def save(report: EvalReport, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{report.created_at:%Y%m%dT%H%M%S}-{report.config_hash}.json"
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
