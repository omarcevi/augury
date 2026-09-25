import httpx
import pytest

from augury.core.config import HttpConfig
from augury.sources.http import (
    HttpError,
    PoliteClient,
    ResponseTooLarge,
    RobotsDisallowed,
    user_agent,
)
from tests.helpers import FakeTime, allow_robots

ORIGIN = "https://example.com"


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
async def client(fake_time):
    async with PoliteClient(
        HttpConfig(retries=2, max_bytes=1000),
        sleep=fake_time.sleep,
        clock=fake_time.clock,
        rng=lambda: 0.0,
    ) as c:
        yield c


async def test_sends_identifying_user_agent(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(f"{ORIGIN}/a").mock(return_value=httpx.Response(200, text="ok"))
    resp = await client.get(f"{ORIGIN}/a")
    assert resp.text() == "ok"
    assert route.calls.last.request.headers["user-agent"] == user_agent()
    assert user_agent().startswith("augury/")


@pytest.mark.respx(assert_all_called=False)  # the blocked route must stay uncalled
async def test_robots_disallow_blocks_the_request(client, respx_mock):
    allow_robots(respx_mock, ORIGIN, "User-agent: *\nDisallow: /private\n")
    route = respx_mock.get(f"{ORIGIN}/private/x")
    with pytest.raises(RobotsDisallowed):
        await client.get(f"{ORIGIN}/private/x")
    assert not route.called


async def test_missing_robots_allows_everything(client, respx_mock):
    respx_mock.get(f"{ORIGIN}/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(f"{ORIGIN}/a").mock(return_value=httpx.Response(200))
    assert (await client.get(f"{ORIGIN}/a")).status == 200


async def test_crawl_delay_spaces_requests(client, respx_mock, fake_time):
    allow_robots(respx_mock, ORIGIN, "User-agent: *\nCrawl-delay: 15\n")
    respx_mock.get(url__startswith=f"{ORIGIN}/p").mock(return_value=httpx.Response(200))
    waits: list[tuple[str, float]] = []
    client.on_wait = lambda host, s: waits.append((host, s))
    await client.get(f"{ORIGIN}/p1")
    await client.get(f"{ORIGIN}/p2")
    assert waits and waits[-1][0] == "example.com" and waits[-1][1] == pytest.approx(15.0)


async def test_retry_after_is_honoured(client, respx_mock, fake_time):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/busy").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, text="done"),
        ]
    )
    assert (await client.get(f"{ORIGIN}/busy")).text() == "done"
    assert 2.0 in fake_time.sleeps


async def test_server_errors_give_up_after_retries(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(f"{ORIGIN}/down").mock(return_value=httpx.Response(503))
    with pytest.raises(HttpError) as err:
        await client.get(f"{ORIGIN}/down")
    assert err.value.status == 503 and route.call_count == 3


async def test_timeouts_are_retried(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/slow").mock(
        side_effect=[
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, text="late"),
        ]
    )
    assert (await client.get(f"{ORIGIN}/slow")).text() == "late"


async def test_conditional_get_returns_not_modified(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    route = respx_mock.get(f"{ORIGIN}/feed").mock(return_value=httpx.Response(304))
    resp = await client.get(
        f"{ORIGIN}/feed", etag='"v1"', last_modified="Wed, 24 Sep 2026 07:00:00 GMT"
    )
    assert resp.not_modified
    assert route.calls.last.request.headers["if-none-match"] == '"v1"'


async def test_oversized_response_is_rejected(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/big").mock(return_value=httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(ResponseTooLarge):
        await client.get(f"{ORIGIN}/big")


async def test_redirect_loop_is_an_http_error(client, respx_mock):
    allow_robots(respx_mock, ORIGIN)
    respx_mock.get(f"{ORIGIN}/a").mock(return_value=httpx.Response(302, headers={"Location": "/b"}))
    respx_mock.get(f"{ORIGIN}/b").mock(return_value=httpx.Response(302, headers={"Location": "/a"}))
    with pytest.raises(HttpError, match="too many redirects"):
        await client.get(f"{ORIGIN}/a")


async def test_sitemaps_come_from_robots(client, respx_mock):
    allow_robots(
        respx_mock, ORIGIN, "User-agent: *\nAllow: /\nSitemap: https://example.com/sm.xml\n"
    )
    assert await client.sitemaps(f"{ORIGIN}/blog") == ["https://example.com/sm.xml"]
