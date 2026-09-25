import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from augury.core.clock import local_day
from augury.core.db.connect import transaction
from augury.core.db.items_repo import ItemsRepo
from augury.core.models import MAX_SUMMARY_CHARS, NormalizedItem, RawItem
from augury.core.text import (
    canonical_url,
    content_hash,
    extract_arxiv_id,
    item_id_for,
    strip_control_chars,
)


def normalize(raw: RawItem) -> NormalizedItem:
    title = " ".join(strip_control_chars(raw.title).split())
    if not title:
        raise ValueError(f"item from {raw.source_id} has an empty title: {raw.url}")
    summary = strip_control_chars(raw.summary).strip()[:MAX_SUMMARY_CHARS]
    url = raw.url.strip()
    arxiv_id = raw.arxiv_id or extract_arxiv_id(url, summary)
    return NormalizedItem(
        id=item_id_for(raw.kind, url, arxiv_id),
        source_id=raw.source_id,
        kind=raw.kind,
        title=title,
        url=url,
        canonical_url=canonical_url(url),
        authors=[a for a in (" ".join(strip_control_chars(x).split()) for x in raw.authors) if a],
        published_at=raw.published_at,
        summary=summary,
        arxiv_id=arxiv_id,
        image_url=raw.image_url,
        content_hash=content_hash(title, summary),
        signals=raw.signals,
        rank=raw.rank,
    )


@dataclass
class StoreResult:
    seen: int = 0
    new: int = 0
    skipped: int = 0
    new_ids: list[str] = field(default_factory=list)


def store_items(conn: sqlite3.Connection, raws: Iterable[RawItem], *, now: datetime) -> StoreResult:
    result = StoreResult()
    repo = ItemsRepo(conn)
    with transaction(conn):
        for raw in raws:
            try:
                n = normalize(raw)
            except ValueError:
                result.skipped += 1
                continue
            item_id, created = repo.upsert(n, now=now)
            repo.record_signals(item_id, local_day(now), n.signals, n.rank)
            result.seen += 1
            if created:
                result.new += 1
                result.new_ids.append(item_id)
    return result
