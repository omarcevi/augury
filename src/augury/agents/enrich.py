import sqlite3
from dataclasses import dataclass
from datetime import datetime

from augury.core.db.items_repo import ItemsRepo
from augury.core.models import MAX_SUMMARY_CHARS
from augury.core.text import strip_control_chars
from augury.extract.markdown_utils import lead_paragraph
from augury.extract.service import get_or_extract
from augury.sources.http import HttpClient


@dataclass
class EnrichResult:
    attempted: int = 0
    enriched: int = 0
    failed: int = 0
    first_error: str | None = None

    @property
    def error(self) -> str | None:
        if not self.failed:
            return None
        return f"{self.failed} of {self.attempted} failed; first: {self.first_error}"


async def enrich_new_articles(
    conn: sqlite3.Connection, http: HttpClient, *, limit: int, now: datetime
) -> EnrichResult:
    result = EnrichResult()
    if limit <= 0:
        return result
    repo = ItemsRepo(conn)
    for item in repo.needing_enrichment(limit):
        result.attempted += 1
        try:
            content = await get_or_extract(conn, http, item, now=now)  # also warms the reader
            summary = lead_paragraph(content.body_md) if content.status == "ok" else ""
            repo.set_enrichment(item.id, strip_control_chars(summary)[:MAX_SUMMARY_CHARS], now=now)
        except Exception as exc:  # one broken item never blocks the others, now or next scout
            result.failed += 1
            result.first_error = result.first_error or f"{item.id}: {type(exc).__name__}: {exc}"
            repo.set_enrichment(item.id, "", now=now)
            continue
        result.enriched += bool(summary)
    return result
