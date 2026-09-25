import sqlite3
from datetime import datetime

from augury.core.db.items_repo import ItemsRepo
from augury.core.models import MAX_SUMMARY_CHARS
from augury.core.text import strip_control_chars
from augury.extract.markdown_utils import lead_paragraph
from augury.extract.service import get_or_extract
from augury.sources.http import HttpClient


async def enrich_new_articles(
    conn: sqlite3.Connection, http: HttpClient, *, limit: int, now: datetime
) -> int:
    if limit <= 0:
        return 0
    repo = ItemsRepo(conn)
    enriched = 0
    for item in repo.needing_enrichment(limit):
        content = await get_or_extract(conn, http, item, now=now)  # also warms the reader cache
        summary = lead_paragraph(content.body_md) if content.status == "ok" else ""
        repo.set_enrichment(item.id, strip_control_chars(summary)[:MAX_SUMMARY_CHARS], now=now)
        enriched += bool(summary)
    return enriched
