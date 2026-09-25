import asyncio
from collections.abc import Callable

import httpx

from augury.sources.http import HttpError, Response


class FakeTime:
    """A clock that only moves when something sleeps, so throttling is testable."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def allow_robots(respx_mock, origin: str, body: str = "User-agent: *\nAllow: /\n") -> None:
    respx_mock.get(f"{origin}/robots.txt").mock(return_value=httpx.Response(200, text=body))


class NullHttp:
    """An HttpClient for tests whose adapters never touch the network."""

    on_wait: Callable[[str, float], None] | None = None

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        raise AssertionError(f"unexpected network call: {url}")

    async def sitemaps(self, url):
        return []


class SlowHttp:
    """An HttpClient whose get() blocks well past any test's window, so a caller can
    reliably catch a fetch in flight (e.g. to exercise cancelling it) instead of racing it."""

    on_wait: Callable[[str, float], None] | None = None

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        self.started.set()
        await asyncio.sleep(30)
        raise AssertionError("SlowHttp.get() should have been cancelled before waking up")

    async def sitemaps(self, url):
        return []


class CountingHttp:
    """Serves canned pages and records each request, so tests can prove when the network is used."""

    def __init__(self, pages: dict[str, bytes] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []
        self.on_wait = None

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        self.calls.append(url)
        if url not in self.pages:
            raise HttpError(url, "HTTP 404", status=404)
        return Response(url, 200, httpx.Headers(), self.pages[url])

    async def sitemaps(self, url):
        return []
