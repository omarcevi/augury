"""Discovery's guards (spec §5.6), all in code. ToolGuard is the before_tool_callback that caps
a run at 20 tool calls and 90 s; when a cap is hit it ends the run, and the candidates tested
so far are offered instead. Any submit_candidates call ends the run too (a malformed one is
answered by ADK without reaching our function), and as the agent's before_model_callback
ToolGuard also caps the model turns themselves. accept_submission is submit_candidates' rule:
only recipes that test_recipe ran successfully in this run, at most 5, samples from our own
test, and a candidate whose URL is already a source marked duplicate. homepage_for keeps a
stored homepage on the recipe's own host."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from google.adk.agents import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from augury.agents.discovery.models import (
    MAX_CANDIDATES,
    Candidate,
    RecipeArg,
    UserRecipe,
    primary_url,
    recipe_hash,
)
from augury.agents.discovery.tools import VerifiedRecipes

MAX_TOOL_CALLS = 20
MAX_SECONDS = 90.0
# Every model turn but the last makes at least one counted tool call (a text reply, a submit
# or a refused call ends the run), so a run needs at most MAX_TOOL_CALLS + 1 turns. The turn
# cap only trips if something slips past before_tool.
MAX_MODEL_TURNS = MAX_TOOL_CALLS + 1
SUBMIT = "submit_candidates"


@dataclass(frozen=True)
class Progress:
    """One live line for the TUI panel or the CLI: a tool starting, or its outcome."""

    tool: str
    detail: str
    done: bool = False
    ok: bool = True


def _short(args: dict[str, Any]) -> str:
    for key in ("url", "query"):
        if isinstance(value := args.get(key), str):
            return value[:120]
    recipe = args.get("recipe")
    if isinstance(recipe, BaseModel):
        recipe = recipe.model_dump()
    if isinstance(recipe, dict):
        kind = recipe.get("type", "?")
        url = recipe.get("feed_url") or recipe.get("sitemap_url") or recipe.get("listing_url")
        return f"{kind} {url or ''}".strip()[:120]
    if isinstance(cands := args.get("candidates"), list):
        return f"{len(cands)} candidate(s)"
    return ""


@dataclass
class ToolGuard:
    max_calls: int = MAX_TOOL_CALLS
    max_seconds: float = MAX_SECONDS
    clock: Callable[[], float] = time.monotonic
    on_progress: Callable[[Progress], None] | None = None
    max_turns: int = MAX_MODEL_TURNS
    calls: int = 0
    turns: int = 0
    stopped: str | None = None  # why the run was cut short, when it was
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def _emit(self, line: Progress) -> None:
        if self.on_progress is not None:
            self.on_progress(line)

    def before_model(
        self, callback_context: Context, llm_request: LlmRequest
    ) -> LlmResponse | None:
        """Refuses a model turn past the cap: the text reply it returns instead ends the run."""
        if self.stopped is None and self.turns >= self.max_turns:
            self.stopped = f"stopped after {self.turns} model turns (the limit)"
        if self.stopped is not None:
            text = f"{self.stopped}; the run is over"
            return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
        self.turns += 1
        return None

    def before_tool(
        self, tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        if tool.name == SUBMIT:
            # The way out: never blocked or counted, and any attempt ends the run. ADK answers a
            # call with missing arguments itself, without calling submit_candidates, so without
            # this a malformed submit would buy the model uncounted, untimed turns. The
            # candidates tested so far are then offered instead.
            tool_context.actions.skip_summarization = True
        else:
            elapsed = self.clock() - self.started
            if self.stopped is None and self.calls >= self.max_calls:
                self.stopped = f"stopped after {self.calls} tool calls (the limit)"
            elif self.stopped is None and elapsed >= self.max_seconds:
                self.stopped = (
                    f"stopped after {elapsed:.0f} s (the limit is {self.max_seconds:.0f} s)"
                )
            if self.stopped is not None:
                tool_context.actions.skip_summarization = True  # ends the agent's turn
                return {"ok": False, "error": f"{self.stopped}; the run is over"}
            self.calls += 1
        self._emit(Progress(tool.name, _short(args)))
        return None

    def after_tool(
        self,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict[str, Any],
    ) -> dict[str, Any] | None:
        ok = bool(tool_response.get("ok", True)) if isinstance(tool_response, dict) else True
        detail = ""
        if isinstance(tool_response, dict):
            if not ok:
                detail = str(tool_response.get("error", ""))[:160]
            elif "items_found" in tool_response:
                detail = f"{tool_response['items_found']} items"
            elif "valid_feeds" in tool_response:
                detail = f"{tool_response['valid_feeds']} valid feed(s)"
            elif "accepted" in tool_response:
                detail = f"{tool_response['accepted']} accepted"
        self._emit(Progress(tool.name, detail, done=True, ok=ok))
        return None


class CandidateArg(BaseModel):
    name: str
    homepage: str = ""
    recipe: RecipeArg
    recipe_hash: str = ""
    sample_items: list[str] = Field(default_factory=list)  # ignored: our test's samples win
    confidence: float = 0.5
    note: str = ""


@dataclass
class Submission:
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    submitted: bool = False


def accept_submission(
    raw: list[Any],
    tested: VerifiedRecipes,
    find_existing: Callable[[str], str | None],
) -> Submission:
    """The candidates that pass the guards, and a reason for each one that doesn't."""
    out = Submission(submitted=True)
    seen: set[str] = set()
    for i, item in enumerate(raw[:MAX_CANDIDATES] if isinstance(raw, list) else []):
        try:
            arg = item if isinstance(item, CandidateArg) else CandidateArg.model_validate(item)
            recipe = arg.recipe.to_recipe()
        except ValidationError as e:
            out.rejected.append(f"candidate {i + 1}: invalid ({e.error_count()} problem(s))")
            continue
        h = recipe_hash(recipe)
        verified = tested.get(h)
        if verified is None:
            out.rejected.append(
                f"candidate {i + 1} ({arg.name[:40]}): its recipe was never tested successfully"
            )
            continue
        if h in seen:
            continue
        seen.add(h)
        candidate = Candidate.build(
            name=arg.name,
            homepage=homepage_for(verified.recipe, arg.homepage),
            recipe=verified.recipe,
            samples=verified.samples,
            confidence=arg.confidence,
            note=arg.note,
        )
        candidate.duplicate_of = find_existing(primary_url(candidate.recipe))
        out.candidates.append(candidate)
    if isinstance(raw, list) and len(raw) > MAX_CANDIDATES:
        out.rejected.append(f"only the first {MAX_CANDIDATES} candidates are kept")
    return out


def fallback_candidates(
    tested: VerifiedRecipes, find_existing: Callable[[str], str | None], name: str
) -> list[Candidate]:
    """When a limit ends the run before submit_candidates: every recipe tested so far."""
    out: list[Candidate] = []
    for verified in list(tested.by_hash.values())[:MAX_CANDIDATES]:
        url = primary_url(verified.recipe)
        candidate = Candidate.build(
            name=name,
            homepage=homepage_for(verified.recipe),
            recipe=verified.recipe,
            samples=verified.samples,
            confidence=0.5,
            note="tested, but the agent stopped before choosing",
        )
        candidate.duplicate_of = find_existing(url)
        out.append(candidate)
    return out


def homepage_for(recipe: UserRecipe, claimed: str = "") -> str:
    """The homepage to store with a recipe: the claimed one (the model's, or a feed's <link>)
    only when it is http(s) on the recipe URL's own host, else that host's root. A homepage
    becomes re-discover's probe target later, so it must never point anywhere the recipe
    doesn't already fetch from (a LAN address, a cloud metadata endpoint)."""
    base = urlsplit(primary_url(recipe))
    root = f"{base.scheme}://{base.netloc}/"
    try:
        parts = urlsplit(claimed.strip())
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return root
    if parts.scheme not in ("http", "https") or parts.hostname != base.hostname:
        return root
    # Only the path and query are kept, on the recipe's own netloc: a claimed port, userinfo
    # or a netloc that another URL parser would read as a different host never gets stored.
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{base.netloc}{parts.path or '/'}{query}"
