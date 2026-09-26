import asyncio
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from augury.llm.resolver import ModelUnavailable, ResolvedModel, Resolver, Role
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

    on_wait: Callable[[str, float], None] | None = None

    def __init__(self, pages: dict[str, bytes] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        self.calls.append(url)
        if url not in self.pages:
            raise HttpError(url, "HTTP 404", status=404)
        return Response(url, 200, httpx.Headers(), self.pages[url])

    async def sitemaps(self, url):
        return []


HF_FIXTURES = Path(__file__).parent / "fixtures" / "hf"
HF_API_PAGES = {
    "https://huggingface.co/api/daily_papers?sort=trending&limit=50": "daily_papers.json",
    "https://huggingface.co/api/blog": "blog.json",
    "https://huggingface.co/api/blog/community?sort=trending": "community.json",
}


class HfHttp(CountingHttp):
    """Serves the captured Hugging Face API responses (any other page 404s). A request waits
    while its gate is cleared, so a test can catch a scout mid-fetch or mid-enrichment."""

    def __init__(self) -> None:
        super().__init__(
            {url: (HF_FIXTURES / name).read_bytes() for url, name in HF_API_PAGES.items()}
        )
        self.api_gate, self.page_gate = asyncio.Event(), asyncio.Event()
        self.api_gate.set()
        self.page_gate.set()
        self.api_started, self.page_started = asyncio.Event(), asyncio.Event()

    async def get(self, url, *, etag=None, last_modified=None, respect_robots=True):
        started, gate = (
            (self.api_started, self.api_gate)
            if url in self.pages
            else (self.page_started, self.page_gate)
        )
        started.set()
        await gate.wait()
        return await super().get(url)


class ScriptedLlm(BaseLlm):
    """ADK's BaseLlm with canned replies (spec §13). Each reply is a str, a types.Part (e.g. a
    function call), an exception to raise, or a function of the request that returns a str.
    Every request is recorded, so tests can prove when a model was (or wasn't) called."""

    model: str = "fake-model"
    replies: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)
    usage: tuple[int, int] = (100, 20)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request)
        if not self.replies:
            raise AssertionError("ScriptedLlm has no reply left for this request")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, types.Part):
            part = reply
        else:
            part = types.Part(text=str(reply(llm_request)) if callable(reply) else str(reply))
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.usage[0], candidates_token_count=self.usage[1]
            ),
        )

    @property
    def prompts(self) -> list[str]:
        return [request_text(r) for r in self.requests]


def request_text(request: LlmRequest) -> str:
    """The text of a request's last user message: the prompt the node sent."""
    for content in reversed(request.contents):
        if content.role == "user":
            return "".join(part.text or "" for part in content.parts or [])
    return ""


def fake_resolver(
    llm: BaseLlm | None = None,
    *,
    spec: str = "fake/fake-model",
    native: bool = False,
    unavailable: str | None = None,
) -> Resolver:
    """Serves every role with `llm`, or refuses every role with `unavailable` as the reason."""
    model = llm or ScriptedLlm()

    def resolve(role: Role, agent: str | None = None) -> ResolvedModel:
        if unavailable is not None:
            raise ModelUnavailable(unavailable)
        return ResolvedModel(spec=spec, provider=spec.partition("/")[0], native=native, llm=model)

    return resolve
