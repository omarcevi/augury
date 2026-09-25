import sqlite3
from datetime import datetime, timedelta

from augury.core.db.contents_repo import ContentsRepo
from augury.core.models import Content, Item
from augury.extract import extract_item
from augury.extract.base import ExtractionError, UnsupportedItem
from augury.sources.http import HttpClient, HttpError

EXTRACTOR_VERSION = 1  # bump when extraction changes, so cached bodies are rebuilt
RETRY_FAILED_AFTER = timedelta(hours=1)
PAYWALL_STATUSES = frozenset({401, 402, 403})


async def get_or_extract(
    conn: sqlite3.Connection, http: HttpClient, item: Item, *, now: datetime, refresh: bool = False
) -> Content:
    repo = ContentsRepo(conn)
    cached = repo.get(item.id)
    if cached and not refresh:
        if cached.status == "ok" and cached.extractor_version == EXTRACTOR_VERSION:
            return cached
        if cached.status != "ok" and now - cached.fetched_at < RETRY_FAILED_AFTER:
            return cached
    content = await _extract(http, item, now)
    repo.save(content)
    return content


async def _extract(http: HttpClient, item: Item, now: datetime) -> Content:
    base = {"item_id": item.id, "extractor_version": EXTRACTOR_VERSION, "fetched_at": now}
    try:
        extracted = await extract_item(http, item)
    except HttpError as e:
        status = "paywalled" if e.status in PAYWALL_STATUSES else "failed"
        return Content(status=status, extractor="none", error=str(e), **base)
    except (ExtractionError, UnsupportedItem) as e:
        return Content(status="failed", extractor="none", error=str(e), **base)
    return Content(
        status="ok",
        body_md=extracted.body_md,
        extractor=extracted.extractor,
        word_count=extracted.word_count,
        **base,
    )
