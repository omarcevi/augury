"""web_search for discovery (spec §5.6). `gemini` is ADK's google_search grounding in its own
sub-agent, wrapped as an AgentTool (built-in search can't share an agent with function tools).
The others are one function tool over a WebSearcher: ddgs (no key), Tavily or Exa (keys from
the environment). Tavily and Exa are APIs we call with the user's key, not pages we crawl, so
they use a plain httpx client rather than PoliteClient's robots rules."""

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, override

import httpx
from google.adk.agents import LlmAgent
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel

from augury.agents.discovery.models import MAX_URL_CHARS, SearchHit
from augury.core.config import SearchConfig
from augury.llm.budget import UsageLedger
from augury.llm.resolver import ResolvedModel
from augury.llm.safe_prompt import prompt
from augury.sources.http import user_agent

SearchProvider = Literal["gemini", "duckduckgo", "tavily", "exa"]
KEY_VARS: dict[str, str] = {"tavily": "TAVILY_API_KEY", "exa": "EXA_API_KEY"}
API_TIMEOUT_S = 20.0
MAX_API_BYTES = 2_000_000
SNIPPET_CHARS = 300
# One tool result, serialized, stays within MAX_RESULT_CHARS (~4k tokens): the budget is checked
# before each model call, so a single huge result would be one call it can't stop. The fenced
# block gets FENCED_CHARS of it, and our own fields (ok, a 300-char error, counts) the rest.
MAX_RESULT_CHARS = 16_000
FENCED_CHARS = 15_000
TRUNCATED = "\n[truncated: the rest didn't fit in a tool result]"


class SearchUnavailable(Exception):
    """The configured search provider can't run (a missing key, a non-Gemini smart model)."""


class SearchError(Exception):
    """One search failed; the agent sees the message and can try something else."""


class WebSearcher(Protocol):
    name: str

    async def search(self, query: str) -> list[SearchHit]: ...


def resolve_provider(config: SearchConfig, smart: ResolvedModel) -> SearchProvider:
    if config.provider == "auto":
        return "gemini" if smart.native else "duckduckgo"
    if config.provider == "gemini" and not smart.native:
        raise SearchUnavailable(
            f"[search] provider = gemini needs a gemini/ or vertex_ai/ smart model, not "
            f"{smart.spec}; use auto, duckduckgo, tavily or exa"
        )
    return config.provider


def _serialized_len(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False)) - 2


def fence(value: object, limit: int = FENCED_CHARS) -> str:
    """Web-derived text for a tool result: a data block the model is told not to obey, cut
    (with a marker) so that the block, serialized, fits in `limit` characters."""
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    fenced = prompt(t"{data}")
    if _serialized_len(fenced) <= limit:
        return fenced

    def fits(keep: int) -> bool:
        cut = data[:keep] + TRUNCATED
        return _serialized_len(prompt(t"{cut}")) <= limit

    low, high = 0, min(len(data), limit)  # the longest prefix that fits: a character or more
    while low < high:  # serializes to 1 to 6, so search for it
        mid = (low + high + 1) // 2
        low, high = (mid, high) if fits(mid) else (low, mid - 1)
    cut = data[:low] + TRUNCATED
    return prompt(t"{cut}")


def _hit(title: object, url: object, snippet: object) -> SearchHit | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if len(url) > MAX_URL_CHARS:
        return None
    return SearchHit(
        title=str(title or "")[:200], url=url, snippet=str(snippet or "")[:SNIPPET_CHARS]
    )


def _ddgs_text(query: str, max_results: int) -> list[dict[str, Any]]:
    from ddgs import DDGS  # imported here: it loads its search engines on import

    return DDGS(timeout=10).text(query, max_results=max_results)


class DdgsSearcher:
    name = "duckduckgo"

    def __init__(
        self,
        max_results: int,
        backend: Callable[[str, int], list[dict[str, Any]]] = _ddgs_text,
    ) -> None:
        self.max_results, self._backend = max_results, backend

    async def search(self, query: str) -> list[SearchHit]:
        try:  # ddgs is synchronous; a thread keeps the TUI responsive
            rows = await asyncio.to_thread(self._backend, query, self.max_results)
        except Exception as e:  # ddgs raises its own DDGSException family, and more
            raise SearchError(f"duckduckgo search failed: {type(e).__name__}: {e}") from e
        hits = [_hit(r.get("title"), r.get("href"), r.get("body")) for r in rows]
        return [h for h in hits if h is not None][: self.max_results]


class _ApiSearcher:
    name = ""
    url = ""

    def __init__(
        self, key: str, max_results: int, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._key, self.max_results, self._transport = key, max_results, transport

    def _request(self, query: str) -> tuple[dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    def _hits(self, data: Any) -> list[SearchHit]:
        raise NotImplementedError

    async def search(self, query: str) -> list[SearchHit]:
        headers, body = self._request(query)
        try:
            async with httpx.AsyncClient(
                timeout=API_TIMEOUT_S,
                headers={"User-Agent": user_agent()},
                transport=self._transport,
            ) as client:
                resp = await client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise SearchError(f"{self.name} search failed: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise SearchError(f"{self.name} search failed: HTTP {resp.status_code}")
        if len(resp.content) > MAX_API_BYTES:
            raise SearchError(f"{self.name} answered with more than {MAX_API_BYTES} bytes")
        try:
            return self._hits(resp.json())[: self.max_results]
        except (ValueError, TypeError, AttributeError) as e:
            raise SearchError(f"{self.name} answered in an unexpected shape") from e


class TavilySearcher(_ApiSearcher):
    name = "tavily"
    url = "https://api.tavily.com/search"

    def _request(self, query: str) -> tuple[dict[str, str], dict[str, Any]]:
        return {"Authorization": f"Bearer {self._key}"}, {
            "query": query,
            "max_results": self.max_results,
        }

    def _hits(self, data: Any) -> list[SearchHit]:
        hits = [_hit(r.get("title"), r.get("url"), r.get("content")) for r in data["results"]]
        return [h for h in hits if h is not None]


class ExaSearcher(_ApiSearcher):
    name = "exa"
    url = "https://api.exa.ai/search"

    def _request(self, query: str) -> tuple[dict[str, str], dict[str, Any]]:
        return {"x-api-key": self._key}, {"query": query, "numResults": self.max_results}

    def _hits(self, data: Any) -> list[SearchHit]:
        hits = [
            _hit(r.get("title"), r.get("url"), r.get("text") or r.get("summary"))
            for r in data["results"]
        ]
        return [h for h in hits if h is not None]


def make_searcher(
    provider: SearchProvider, config: SearchConfig, env: Mapping[str, str] | None = None
) -> WebSearcher:
    """A searcher for the function-tool providers (not gemini, which is an AgentTool)."""
    env = os.environ if env is None else env
    if provider == "duckduckgo":
        return DdgsSearcher(config.max_results)
    if provider in KEY_VARS:
        var = KEY_VARS[provider]
        if not (key := env.get(var)):
            raise SearchUnavailable(f"[search] provider = {provider} needs {var} in .env")
        cls = TavilySearcher if provider == "tavily" else ExaSearcher
        return cls(key, config.max_results)
    raise SearchUnavailable(f"{provider} search is not a function tool")


def search_tool(searcher: WebSearcher):
    """web_search as a plain function tool (ddgs, Tavily, Exa)."""

    async def web_search(query: str) -> dict[str, Any]:
        """Search the web. Returns results with a title, url and snippet. Use it to find a
        publication's homepage or blog when you only know its name."""
        try:
            hits = await searcher.search(query)
        except SearchError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "results": fence([h.model_dump() for h in hits])}

    return web_search


SEARCH_INSTRUCTION = (
    "You search the web with Google for the query in the request. Reply with up to 8 of the "
    "most relevant results, one per line, as: title | the page's full URL | one short line on "
    "what it is. Prefer official blogs and publications. Only list URLs the search returned."
)


class SearchRequest(BaseModel):
    query: str


class FencedAgentTool(AgentTool):
    """An AgentTool whose answer (grounded web text) reaches the parent as fenced data."""

    @override
    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        result = await super().run_async(args=args, tool_context=tool_context)
        return {"ok": True, "results": fence(result)}


def google_search_tool(model: ResolvedModel, ledger: UsageLedger | None) -> AgentTool:
    """web_search through Gemini's Google Search grounding, on the same smart model, with the
    same budget gate and usage ledger as the discovery agent itself."""
    agent = LlmAgent(
        name="web_search",
        model=model.llm,
        description="Search the web with Google. Returns relevant pages with their URLs.",
        instruction=lambda _ctx: SEARCH_INSTRUCTION,
        input_schema=SearchRequest,
        tools=[google_search],
        before_model_callback=ledger.before_model if ledger else None,
        after_model_callback=ledger.after_model if ledger else None,
    )
    return FencedAgentTool(agent=agent, propagate_grounding_metadata=True)
