from collections.abc import Callable

import httpx


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
