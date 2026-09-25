import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from click.testing import CliRunner

from augury import extract
from augury.agents.normalize import store_items
from augury.cli import main
from augury.core.db.contents_repo import ContentsRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.models import Content, Item, RawItem
from augury.extract.pdf import pdf_to_markdown
from augury.extract.service import EXTRACTOR_VERSION, get_or_extract
from augury.sources.http import PoliteClient
from tests.helpers import FakeTime, allow_robots
from tests.pdf_builder import make_pdf
from tests.unit.test_extract_html import GENERIC
from tests.unit.test_paper_extract import MINI, THIN_STUB

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
URL = "https://example.com/post"


def seed_article(conn, url: str = URL) -> Item:
    item_id = store_items(
        conn, [RawItem(source_id="hf-blog", url=url, title="Post")], now=NOW
    ).new_ids[0]
    item = ItemsRepo(conn).get(item_id)
    assert item is not None
    return item


def seed_paper(conn, arxiv_id: str = "2609.00001") -> Item:
    raw = RawItem(
        source_id="hf-papers",
        kind="paper",
        arxiv_id=arxiv_id,
        title="A Tiny Paper",
        url=f"https://huggingface.co/papers/{arxiv_id}",
    )
    item = ItemsRepo(conn).get(store_items(conn, [raw], now=NOW).new_ids[0])
    assert item is not None
    return item


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


async def test_successful_extraction_is_cached(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_article(conn)
    allow_robots(respx_mock, "https://example.com")
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=GENERIC))
    first = await get_or_extract(conn, http, item, now=NOW)
    second = await get_or_extract(conn, http, item, now=NOW + timedelta(days=3))
    assert first.status == second.status == "ok" and route.call_count == 1


async def test_forbidden_page_is_marked_paywalled(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_article(conn)
    allow_robots(respx_mock, "https://example.com")
    respx_mock.get(URL).mock(return_value=httpx.Response(403))
    content = await get_or_extract(conn, http, item, now=NOW)
    assert content.status == "paywalled" and "403" in (content.error or "")


async def test_failures_are_retried_only_after_an_hour(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_article(conn)
    allow_robots(respx_mock, "https://example.com")
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"<p>tiny</p>"))
    await get_or_extract(conn, http, item, now=NOW)
    await get_or_extract(conn, http, item, now=NOW + timedelta(minutes=10))
    assert route.call_count == 1
    await get_or_extract(conn, http, item, now=NOW + timedelta(hours=2))
    assert route.call_count == 2


async def test_stale_extractor_version_is_re_extracted(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_article(conn)
    allow_robots(respx_mock, "https://example.com")
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=GENERIC))
    ContentsRepo(conn).save(
        Content(
            item_id=item.id,
            status="ok",
            body_md="stale body from an older extractor",
            extractor="trafilatura",
            extractor_version=EXTRACTOR_VERSION - 1,
            word_count=100,
            fetched_at=NOW,
        )
    )
    content = await get_or_extract(conn, http, item, now=NOW)
    assert content.status == "ok" and route.call_count == 1
    assert content.extractor_version == EXTRACTOR_VERSION


def test_dev_extract_prints_markdown(fast_http, respx_mock):
    conn = open_db(fast_http, now=NOW)
    item = seed_article(conn)
    conn.close()
    allow_robots(respx_mock, "https://example.com")
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=GENERIC))
    result = CliRunner().invoke(main, ["dev", "extract", item.id])
    assert result.exit_code == 0, result.output
    assert "draft model propose tokens" in result.output


async def test_paper_uses_arxiv_html(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_paper(conn)
    allow_robots(respx_mock, "https://arxiv.org")
    respx_mock.get("https://arxiv.org/html/2609.00001").mock(
        return_value=httpx.Response(200, content=MINI)
    )
    content = await get_or_extract(conn, http, item, now=NOW)
    assert content.status == "ok" and content.extractor == "arxiv_html"


async def test_paper_without_html_falls_back_to_pdf(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_paper(conn)
    allow_robots(respx_mock, "https://arxiv.org")
    respx_mock.get("https://arxiv.org/html/2609.00001").mock(return_value=httpx.Response(404))
    respx_mock.get("https://arxiv.org/pdf/2609.00001").mock(
        return_value=httpx.Response(
            200, content=make_pdf(["Speculative decoding verifies draft tokens in parallel."] * 10)
        )
    )
    content = await get_or_extract(conn, http, item, now=NOW)
    assert content.status == "ok" and content.extractor == "pdf"


async def test_pdf_parsing_runs_off_the_event_loop(paths, http, respx_mock, monkeypatch):
    threads: list[threading.Thread] = []

    def record(content: bytes) -> str:
        threads.append(threading.current_thread())
        return pdf_to_markdown(content)

    monkeypatch.setattr(extract, "pdf_to_markdown", record)
    conn = open_db(paths, now=NOW)
    item = seed_paper(conn)
    allow_robots(respx_mock, "https://arxiv.org")
    respx_mock.get("https://arxiv.org/html/2609.00001").mock(return_value=httpx.Response(404))
    respx_mock.get("https://arxiv.org/pdf/2609.00001").mock(
        return_value=httpx.Response(
            200, content=make_pdf(["Speculative decoding verifies draft tokens in parallel."] * 10)
        )
    )
    assert (await get_or_extract(conn, http, item, now=NOW)).extractor == "pdf"
    assert threads and threads[0] is not threading.main_thread()  # the TUI stays responsive


async def test_thin_arxiv_html_falls_back_to_pdf(paths, http, respx_mock):
    conn = open_db(paths, now=NOW)
    item = seed_paper(conn)
    allow_robots(respx_mock, "https://arxiv.org")
    respx_mock.get("https://arxiv.org/html/2609.00001").mock(
        return_value=httpx.Response(200, content=THIN_STUB)
    )
    pdf_route = respx_mock.get("https://arxiv.org/pdf/2609.00001").mock(
        return_value=httpx.Response(
            200, content=make_pdf(["Speculative decoding verifies draft tokens in parallel."] * 10)
        )
    )
    content = await get_or_extract(conn, http, item, now=NOW)
    assert content.status == "ok" and content.extractor == "pdf"
    assert pdf_route.call_count == 1
