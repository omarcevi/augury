"""Shared by the RAG tests: a database with items, and embedders that fail on purpose."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.llm.embedder import Embedded, EmbedTask, HashEmbedder

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def add_items(
    conn: sqlite3.Connection,
    entries: Sequence[tuple[str, str]],
    *,
    source_id: str = "hf-blog",
    kind: str = "article",
    now: datetime = NOW,
    published_at: datetime | None = None,
    arxiv_ids: Sequence[str | None] | None = None,
) -> list[str]:
    """(title, summary) pairs stored as items of `source_id`; returns their ids in order."""
    raws = [
        RawItem(
            source_id=source_id,
            url=f"https://example.com/{source_id}/{n}-{title.replace(' ', '-')}",
            title=title,
            summary=summary,
            kind=kind,  # type: ignore[arg-type]
            published_at=published_at,
            arxiv_id=arxiv_ids[n] if arxiv_ids else None,
        )
        for n, (title, summary) in enumerate(entries)
    ]
    result = store_items(conn, raws, now=now)
    return result.new_ids


class BrokenEmbedder(HashEmbedder):
    """Raises on every call, like a provider that is down."""

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded:
        raise RuntimeError("provider down")


class PricedHash(HashEmbedder):
    """A hash embedder that reports tokens, so the budget and usage paths run."""

    def __init__(self, dimensions: int = 16) -> None:
        super().__init__(dimensions=dimensions, spec="gemini/gemini-embedding-001")

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> Embedded:
        out = await super().embed(texts, task)
        return Embedded(out.vectors, tokens=100 * len(texts))
