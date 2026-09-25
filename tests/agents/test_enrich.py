from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest

from augury.agents import scout
from augury.agents.enrich import enrich_new_articles
from augury.agents.normalize import store_items
from augury.agents.scout import ScoutDeps, run_scout
from augury.core.config import Config
from augury.core.db.contents_repo import ContentsRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.models import FetchResult, FetchState, RawItem, Source
from augury.sources.http import HttpClient, PoliteClient
from tests.helpers import CountingHttp, FakeTime, allow_robots
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
    assert (await enrich_new_articles(conn, http, limit=10, now=NOW)).enriched == 1
    assert ItemsRepo(conn).get(item_id).summary.startswith("Speculative decoding lets")  # type: ignore[union-attr]
    assert ContentsRepo(conn).get(item_id).status == "ok"  # type: ignore[union-attr]  # reader cache is warm


async def test_a_failing_page_is_attempted_once(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    store_items(conn, [_article("gone")], now=NOW)
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(f"{ORIGIN}/gone").mock(return_value=httpx.Response(404))
    first = await enrich_new_articles(conn, http, limit=10, now=NOW)
    assert (first.enriched, first.failed, first.error) == (0, 0, None)  # a 404 is no crash
    assert (await enrich_new_articles(conn, http, limit=10, now=NOW)).attempted == 0
    assert route.call_count == 1


class PoisonHttp(CountingHttp):
    async def get(self, url, **kwargs):
        if url.endswith("/poison"):
            self.calls.append(url)
            raise RuntimeError("not an HttpError")
        return await super().get(url, **kwargs)


async def test_a_crashing_item_neither_blocks_the_rest_nor_comes_back(paths):
    conn = open_db(paths, now=NOW)
    good = store_items(conn, [_article("good")], now=NOW).new_ids[0]
    later = NOW + timedelta(minutes=1)  # newest first: the poison item is tried first
    poison = store_items(conn, [_article("poison")], now=later).new_ids[0]
    http = PoisonHttp({f"{ORIGIN}/good": GENERIC})
    result = await enrich_new_articles(conn, http, limit=10, now=later)
    assert (result.enriched, result.failed, result.attempted) == (1, 1, 2)
    assert result.error == f"1 of 2 failed; first: {poison}: RuntimeError: not an HttpError"
    assert ItemsRepo(conn).get(good).summary.startswith("Speculative decoding lets")  # type: ignore[union-attr]
    again = await enrich_new_articles(conn, http, limit=10, now=later)
    assert again.attempted == 0 and http.calls.count(f"{ORIGIN}/poison") == 1


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
    assert (await enrich_new_articles(conn, http, limit=2, now=NOW)).enriched == 2
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


async def test_enrichment_crashing_is_reported_but_keeps_the_items(paths, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("enrich exploded")

    monkeypatch.setattr(scout, "enrich_new_articles", boom)
    deps = ScoutDeps(
        conn=open_db(paths, now=NOW),
        http=CountingHttp(),
        config=Config(),
        lock_path=paths.scout_lock_file,
        adapters={"hf_blog": OneArticle()},
        now=lambda: NOW,
    )
    report = await run_scout(deps, only="hf-blog")
    assert report.status == "ok" and report.new_items == 1
    assert report.enrich_error == "RuntimeError: enrich exploded"
    run = RunsRepo(deps.conn).last("scout")
    assert run is not None and run.error == "enrich: RuntimeError: enrich exploded"
    assert run.stats["enrich_error"] == "RuntimeError: enrich exploded"
