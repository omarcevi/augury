import json
from pathlib import Path

import httpx
import pytest

from augury.core.models import FetchState
from augury.sources.base import AdapterError
from augury.sources.builtin import BUILTIN_SOURCES
from augury.sources.http import PoliteClient
from augury.sources.registry import adapter_for
from tests.helpers import FakeTime, allow_robots

HF = "https://huggingface.co"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "hf"
SOURCES = {s.id: s for s in BUILTIN_SOURCES}


@pytest.fixture
async def http():
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as c:
        yield c


async def _fetch(source_id: str, http, respx_mock, payload):
    allow_robots(respx_mock, HF)
    respx_mock.get(url__startswith=f"{HF}/api/").mock(
        return_value=httpx.Response(200, json=payload)
    )
    source = SOURCES[source_id]
    return await adapter_for(source).fetch(source, FetchState(), http)


async def test_papers_map_fields_and_skip_broken_entries(http, respx_mock):
    payload = [
        {
            "paper": {
                "id": "2609.24984",
                "title": "WorldCrafter",
                "summary": "We present…",
                "publishedAt": "2026-09-21T00:00:00.000Z",
                "upvotes": 145,
                "githubStars": 2100,
                "authors": [{"name": "Wangbo Yu", "hidden": False}, {"name": "X", "hidden": True}],
            },
            "numComments": 4,
            "thumbnail": "https://cdn/x.png",
        },
        {"paper": {"id": "2412.20138"}},
    ]
    result = await _fetch("hf-papers", http, respx_mock, payload)
    assert result.skipped == 1 and len(result.items) == 1
    item = result.items[0]
    assert item.kind == "paper" and item.arxiv_id == "2609.24984"
    assert item.url == "https://huggingface.co/papers/2609.24984"
    assert item.authors == ["Wangbo Yu"]
    assert (item.signals.upvotes, item.signals.github_stars, item.signals.comments) == (
        145,
        2100,
        4,
    )
    assert item.rank == 1 and item.published_at is not None


async def test_blog_entries_have_absolute_urls_and_no_summary(http, respx_mock):
    payload = {
        "allBlogs": [
            {
                "title": "Accelerating VLMs",
                "url": "/blog/LiquidAI/lfm2-5-vl-dspark",
                "publishedAt": "2026-09-24T14:08:57.587Z",
                "upvotes": 5,
                "upvotes7d": 5,
                "authorsData": [{"fullname": "Liquid AI", "name": "LiquidAI"}],
            }
        ]
    }
    item = (await _fetch("hf-blog", http, respx_mock, payload)).items[0]
    assert item.url == "https://huggingface.co/blog/LiquidAI/lfm2-5-vl-dspark"
    assert item.summary == "" and item.authors == ["Liquid AI"] and item.signals.upvotes7d == 5


async def test_community_respects_limit(http, respx_mock):
    post = {"title": "Post", "url": "/blog/nvidia/p", "publishedAt": "2026-09-23T00:00:00Z"}
    payload = {"posts": [dict(post, url=f"/blog/u/p{i}") for i in range(30)]}
    assert len((await _fetch("hf-community", http, respx_mock, payload)).items) == 20


async def test_empty_listing_is_an_error(http, respx_mock):
    with pytest.raises(AdapterError, match="0 entries"):
        await _fetch("hf-papers", http, respx_mock, [])


async def test_unexpected_shape_is_an_error(http, respx_mock):
    with pytest.raises(AdapterError):
        await _fetch("hf-blog", http, respx_mock, [1, 2, 3])


@pytest.mark.parametrize(
    ("source_id", "fixture"),
    [
        ("hf-papers", "daily_papers.json"),
        ("hf-blog", "blog.json"),
        ("hf-community", "community.json"),
    ],
)
async def test_real_captured_payloads_parse_without_skips(source_id, fixture, http, respx_mock):
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    result = await _fetch(source_id, http, respx_mock, payload)
    assert result.items and result.skipped == 0
