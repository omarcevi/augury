"""Prefetch (spec §5.2, §5.5): after ranking, read ahead the top N digest items (full text into
the reader cache) and make their TL;DRs, so the first Enter of the day is instant."""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from augury.agents.llm_step import Call, describe, is_schema_failure
from augury.agents.summarize import Summarizer
from augury.core.db.digest_repo import DigestRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import HIDING_FLAGS, Item
from augury.extract.service import get_or_extract
from augury.sources.http import HttpClient


class PrefetchStats(BaseModel):
    extracted: int = 0
    summarized: int = 0
    stopped: str | None = None  # why it stopped early (budget, provider)
    error: str | None = None  # the first item whose TL;DR failed; the others still ran


def top_items(conn: sqlite3.Connection, day: date, limit: int) -> list[Item]:
    """The day's digest from the top, skipping what triage hid (spec §5.4)."""
    items, triage = ItemsRepo(conn), TriageRepo(conn)
    picked: list[Item] = []
    for row in DigestRepo(conn).for_day(day):
        if len(picked) >= limit:
            break
        result = triage.get(row.item_id)
        if result is not None and HIDING_FLAGS & set(result.flags):
            continue
        if (item := items.get(row.item_id)) is not None:
            picked.append(item)
    return picked


async def prefetch_top(
    conn: sqlite3.Connection,
    http: HttpClient,
    summarizer: Summarizer,
    *,
    day: date,
    limit: int,
    run_id: str,
    make_call: Callable[[LlmAgent], Call],
    now: Callable[[], datetime],
) -> PrefetchStats:
    stats = PrefetchStats()
    for item in top_items(conn, day, limit):
        content = await get_or_extract(conn, http, item, now=now())
        if content.status != "ok":
            continue  # the reader shows the summary and offers the browser (spec §11)
        stats.extracted += 1
        if summarizer.cached(item.id) is not None:
            continue
        if (problem := summarizer.budget_problem()) is not None:
            stats.stopped = problem
            break
        try:
            await summarizer.summarize(item, content, run_id=run_id, make_call=make_call)
        except Exception as exc:
            if is_schema_failure(exc):  # a bad reply for this item; the next may be fine
                stats.error = stats.error or f"{item.id}: {describe(exc, 200)}"
                continue
            stats.stopped = describe(exc, 200)  # budget or provider: stop, don't hammer it
            break
        stats.summarized += 1
    return stats
