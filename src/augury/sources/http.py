import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from augury import __version__
from augury.core.config import HttpConfig

UA_TOKEN = "augury"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ROBOTS_TTL_S = 24 * 3600.0
ROBOTS_UNAVAILABLE_TTL_S = 300.0  # robots.txt answered 5xx: RFC 9309 says assume disallow
ROBOTS_NETWORK_TTL_S = 60.0  # short, so a long-lived TUI recovers once the network is back
MAX_RETRY_AFTER_S = 60.0


def user_agent() -> str:
    return f"{UA_TOKEN}/{__version__} (+https://pypi.org/project/augury/)"


class HttpError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{message} ({url})")
        self.url = url
        self.message = message
        self.status = status


class RobotsDisallowed(HttpError):
    pass


class NetworkError(HttpError):
    pass


class ResponseTooLarge(HttpError):
    pass


class NotPublicAddress(HttpError):
    """A fetch a model or a fetched page chose, refused because its host isn't public."""


# A check for every request a call sends (redirect hops and robots.txt included), set per call
# by sources/public_only.py's PublicOnlyHttp. Unset, as for the scout, every host is allowed.
UrlGuard = Callable[[str], Awaitable[None]]
URL_GUARD: ContextVar[UrlGuard | None] = ContextVar("augury_url_guard", default=None)


async def _guard_request(request: httpx.Request) -> None:
    if (guard := URL_GUARD.get()) is not None:
        await guard(str(request.url))  # raises NotPublicAddress before the request is sent


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self) -> Any:
        return json.loads(self.content)

    def text(self) -> str:
        charset = httpx.Headers(self.headers).get("content-type", "")
        encoding = charset.split("charset=")[-1].strip() if "charset=" in charset else "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class HttpClient(Protocol):
    on_wait: Callable[[str, float], None] | None

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        respect_robots: bool = True,
    ) -> Response: ...

    async def sitemaps(self, url: str) -> list[str]: ...


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None means "allow everything", unless there's an error
    fetched_at: float
    ttl: float
    error: str | None = None  # robots.txt couldn't be read, so nothing on the origin is fetched
    network: bool = False


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _check_web_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises on a malformed port
    except ValueError as e:
        raise HttpError(url, f"invalid URL: {e}") from e
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise HttpError(url, "not an http(s) URL")


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


class PoliteClient:
    """The only way this app talks to the network: robots, spacing, retries, size caps."""

    def __init__(
        self,
        cfg: HttpConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.cfg = cfg or HttpConfig()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent()},
            timeout=self.cfg.timeout_s,
            follow_redirects=True,
            max_redirects=self.cfg.max_redirects,
            transport=transport,
            event_hooks={"request": [_guard_request]},  # httpx runs it for every redirect hop
        )
        self._sleep, self._clock, self._rng = sleep, clock, rng
        self._robots: dict[str, _Robots] = {}
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self.on_wait: Callable[[str, float], None] | None = None

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        respect_robots: bool = True,
    ) -> Response:
        _check_web_url(url)  # before robots or retries: neither can help a bad address
        origin = _origin(url)
        interval = self.cfg.min_interval_s
        if respect_robots:
            robots = await self._robots_for(origin)
            if robots.error:
                raise (NetworkError if robots.network else HttpError)(url, robots.error)
            if robots.parser and not robots.parser.can_fetch(UA_TOKEN, url):
                raise RobotsDisallowed(url, "disallowed by robots.txt")
            if robots.parser and (delay := robots.parser.crawl_delay(UA_TOKEN)):
                interval = max(interval, float(delay))
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return await self._get_with_retries(url, origin, interval, headers)

    async def sitemaps(self, url: str) -> list[str]:
        robots = await self._robots_for(_origin(url))
        return list(robots.parser.site_maps() or []) if robots.parser else []

    async def _get_with_retries(
        self, url: str, origin: str, interval: float, headers: dict[str, str]
    ) -> Response:
        attempt = 0
        while True:
            await self._throttle(origin, interval)
            try:
                resp = await self._once(url, headers)
            except httpx.TooManyRedirects as e:
                raise HttpError(url, "too many redirects") from e
            except httpx.InvalidURL as e:
                raise HttpError(url, f"invalid URL: {e}") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt >= self.cfg.retries:
                    raise NetworkError(url, f"network error: {type(e).__name__}") from e
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            if resp.status in RETRY_STATUSES and attempt < self.cfg.retries:
                await self._sleep(
                    _retry_after(resp.headers.get("retry-after")) or self._backoff(attempt)
                )
                attempt += 1
                continue
            if resp.status >= 400:
                raise HttpError(url, f"HTTP {resp.status}", status=resp.status)
            return resp

    async def _once(self, url: str, headers: dict[str, str]) -> Response:
        async with self._client.stream("GET", url, headers=headers) as r:
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self.cfg.max_bytes:
                raise ResponseTooLarge(
                    url, f"response is {declared} bytes (limit {self.cfg.max_bytes})"
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > self.cfg.max_bytes:
                    raise ResponseTooLarge(url, f"response exceeds {self.cfg.max_bytes} bytes")
                chunks.append(chunk)
            return Response(str(r.url), r.status_code, r.headers, b"".join(chunks))

    async def _throttle(self, origin: str, interval: float) -> None:
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:  # scheduling only; the request itself runs outside the lock
            wait = self._next_at.get(origin, 0.0) - self._clock()
            if wait > 0:
                if self.on_wait:
                    self.on_wait(urlsplit(origin).hostname or origin, wait)
                await self._sleep(wait)
            self._next_at[origin] = self._clock() + interval

    def _backoff(self, attempt: int) -> float:
        return min(30.0, 2.0**attempt) * (1 + 0.5 * self._rng())

    async def _robots_for(self, origin: str) -> _Robots:
        # One fetch per origin, even when a scout's parallel sources all start at once.
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            cached = self._robots.get(origin)
            if cached and self._clock() - cached.fetched_at < cached.ttl:
                return cached
            robots = await self._fetch_robots(origin)
            self._robots[origin] = robots
            return robots

    async def _fetch_robots(self, origin: str) -> _Robots:
        now = self._clock()
        try:
            resp = await self._get_with_retries(
                f"{origin}/robots.txt", origin, self.cfg.min_interval_s, {}
            )
        except NotPublicAddress:
            raise  # a refusal for this call only: nothing is cached for the origin
        except NetworkError as e:
            error = f"{e.message} while fetching robots.txt"
            return _Robots(None, now, ROBOTS_NETWORK_TTL_S, error, network=True)
        except HttpError as e:
            if e.status is not None and 400 <= e.status < 500:  # RFC 9309: unavailable → allowed
                return _Robots(None, now, ROBOTS_TTL_S)
            error = f"robots.txt unavailable: {e.message}"  # a 5xx, a size cap, a redirect loop
            return _Robots(None, now, ROBOTS_UNAVAILABLE_TTL_S, error)
        parser = RobotFileParser()
        parser.parse(resp.content.decode("utf-8", errors="replace").splitlines())
        return _Robots(parser, now, ROBOTS_TTL_S)
