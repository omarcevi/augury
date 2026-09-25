from datetime import UTC, datetime
from typing import ClassVar

import httpx
import pytest

from augury.agents.enrich import enrich_new_articles
from augury.agents.normalize import store_items
from augury.agents.scout import ScoutDeps, run_scout
from augury.core.config import Config
from augury.core.db.contents_repo import ContentsRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.models import FetchResult, FetchState, RawItem, Source
from augury.sources.http import HttpClient, PoliteClient
from tests.helpers import FakeTime, allow_robots
from tests.unit.test_extract_html import GENERIC

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
ORIGIN = "https://example.com"


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


def _article(path: str, summary: str = "") -> RawItem:
    return RawItem(source_id="hf-blog", url=f"{ORIGIN}/{path}", title=path, summary=summary)


async def test_articles_without_summary_get_their_opening_paragraph(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [_article("post")], now=NOW).new_ids[0]
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/post").mock(return_value=httpx.Response(200, content=GENERIC))
    assert await enrich_new_articles(conn, http, limit=10, now=NOW) == 1
    assert ItemsRepo(conn).get(item_id).summary.startswith("Speculative decoding lets")  # type: ignore[union-attr]
    assert ContentsRepo(conn).get(item_id).status == "ok"  # type: ignore[union-attr]  # reader cache is warm


async def test_a_failing_page_is_attempted_once(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    store_items(conn, [_article("gone")], now=NOW)
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(f"{ORIGIN}/gone").mock(return_value=httpx.Response(404))
    assert await enrich_new_articles(conn, http, limit=10, now=NOW) == 0
    assert await enrich_new_articles(conn, http, limit=10, now=NOW) == 0
    assert route.call_count == 1


async def test_items_with_summaries_and_papers_are_left_alone(paths, http):
    conn = open_db(paths, now=NOW)
    store_items(
        conn,
        [
            _article("has-summary", summary="Already here."),
            RawItem(
                source_id="hf-papers",
                kind="paper",
                arxiv_id="2609.00001",
                url="https://huggingface.co/papers/2609.00001",
                title="P",
            ),
        ],
        now=NOW,
    )
    assert ItemsRepo(conn).needing_enrichment(10) == []


async def test_limit_is_respected(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    store_items(conn, [_article(f"p{i}") for i in range(3)], now=NOW)
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(url__startswith=f"{ORIGIN}/p").mock(
        return_value=httpx.Response(200, content=GENERIC)
    )
    assert await enrich_new_articles(conn, http, limit=2, now=NOW) == 2
    assert len(ItemsRepo(conn).needing_enrichment(10)) == 1


class OneArticle:
    recipe_type: ClassVar[str] = "fake"

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult:
        return FetchResult(
            items=[_article("post").model_copy(update={"source_id": source.id})], state=state
        )


async def test_scout_reports_enrichment(paths, http, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/post").mock(return_value=httpx.Response(200, content=GENERIC))
    deps = ScoutDeps(
        conn=open_db(paths, now=NOW),
        http=http,
        config=Config(),
        lock_path=paths.scout_lock_file,
        adapters={"hf_blog": OneArticle()},
        now=lambda: NOW,
    )
    report = await run_scout(deps, only="hf-blog")
    assert report.enriched == 1 and report.enrich_error is None
