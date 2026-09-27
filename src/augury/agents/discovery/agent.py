"""Discovery (spec §5.6): a URL or a name in, 1-5 tested candidate sources out. A URL goes to
the code probe first, and feeds it finds become candidates without any LLM call. Otherwise the
smart model runs a tool loop as a standalone ADK agent (the island pattern, spec §3.2) whose
transcript is saved in sessions.db. Nothing is added to sources here: the caller confirms."""

import asyncio
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from augury.agents.discovery.guards import (
    CandidateArg,
    Progress,
    Submission,
    ToolGuard,
    accept_submission,
    fallback_candidates,
    homepage_for,
)
from augury.agents.discovery.models import Candidate, SampleItem, primary_url
from augury.agents.discovery.search import (
    SearchUnavailable,
    WebSearcher,
    google_search_tool,
    make_searcher,
    resolve_provider,
    search_tool,
)
from augury.agents.discovery.tools import DiscoveryTools, VerifiedRecipes
from augury.agents.llm_step import describe, is_budget_stop
from augury.core.config import Config
from augury.core.db.discovery_repo import DiscoveryRepo, DiscoveryStatus
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.models import RssRecipe
from augury.core.text import strip_control_chars
from augury.llm.budget import UsageLedger, budget_problem, today_spend
from augury.llm.probes import ProbeResult
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import ModelUnavailable, Resolver
from augury.llm.safe_prompt import prompt
from augury.sources.http import HttpClient, HttpError
from augury.sources.probe import ProbeAttempt, page_url, probe_url
from augury.sources.probe import ProbeResult as FeedProbe

AGENT = "discovery"
APP_NAME = "augury-discovery"
USER_ID = "local"
HARD_STOP_GRACE_S = 10.0  # past max_seconds, a model call that hangs is cancelled
_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/\S*)?$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)  # mailto:, file:, https:

InputKind = Literal["url", "name"]


def classify_input(text: str) -> tuple[InputKind, str]:
    """("url", address) for anything that looks like a web address, else ("name", text)."""
    value = " ".join(strip_control_chars(text).split())
    if " " not in value and (_SCHEME.match(value) or _DOMAIN.match(value)):
        return "url", value  # a non-web scheme is then refused by page_url, with the reason
    return "name", value


@dataclass
class DiscoveryDeps:
    conn: sqlite3.Connection
    http: HttpClient
    config: Config
    resolver: Resolver
    sessions_db: Path
    now: Callable[[], datetime]
    probes: Mapping[str, ProbeResult] = field(default_factory=dict)  # doctor's cached probes
    env: Mapping[str, str] | None = None  # search keys; None reads os.environ
    clock: Callable[[], float] | None = None  # the guard's timer (tests pass a fake one)
    searcher: WebSearcher | None = None  # replaces the configured non-Gemini provider (tests)


@dataclass
class DiscoveryOutcome:
    query: str
    input_kind: InputKind
    candidates: list[Candidate]
    via: Literal["probe", "agent", "none"]
    run_id: str | None = None
    explanation: str = ""  # why there are few or none, or why the run stopped early
    status: DiscoveryStatus | None = None
    tool_calls: int = 0


def discovery_unavailable(deps: DiscoveryDeps) -> str | None:
    """Why the agent can't run now (spec §11), or None. The URL probe works regardless."""
    try:
        model = deps.resolver("smart", AGENT)
    except ModelUnavailable as e:
        return f"discovery needs the smart model: {e}"
    probe = deps.probes.get("smart")
    if probe is not None and probe.spec == model.spec and not probe.tools:
        return (
            f"{model.spec} couldn't call a tool in the last `augury doctor` probe, and "
            "discovery needs tool calls: pick another smart model in config.toml"
        )
    spend = today_spend(deps.conn, deps.now())
    if (problem := budget_problem(spend, deps.config.budget)) is not None:
        return f"discovery is paused: {problem}"
    return None


def probe_candidates(result: FeedProbe) -> list[Candidate]:
    return [
        Candidate.build(
            name=info.title,
            homepage=homepage_for(RssRecipe(feed_url=info.feed_url), info.homepage),
            recipe=RssRecipe(feed_url=info.feed_url),
            samples=[SampleItem(title=t, url=info.feed_url) for t in info.sample_titles],
            confidence=1.0,
            note=f"feed found by the code probe ({info.entries} entries)",
        )
        for info in result.candidates
    ]


def task_prompt(query: str, probe: FeedProbe | None) -> str:
    if probe is None:
        return prompt(t"Find sources for this publication or topic:\n{query}")
    tried = "\n".join(f"{a.url}: {a.outcome}" for a in probe.attempts[:12]) or "nothing"
    sitemaps = "\n".join(probe.sitemaps[:5]) or "none"
    return prompt(
        t"Find sources for this site. The code probe found no valid feed.\nURL:\n{query}\n"
        t"Probe attempts:\n{tried}\nSitemaps listed in robots.txt:\n{sitemaps}"
    )


def mark_duplicates(conn: sqlite3.Connection, candidates: list[Candidate]) -> list[Candidate]:
    repo = SourcesRepo(conn)
    for c in candidates:
        if c.duplicate_of is None:
            c.duplicate_of = repo.find_by_recipe_url(primary_url(c.recipe))
    return candidates


async def discover(
    query: str,
    deps: DiscoveryDeps,
    *,
    on_progress: Callable[[Progress], None] | None = None,
    probe: FeedProbe | None = None,
    skip_probe: bool = False,
) -> DiscoveryOutcome:
    """Candidates for `query`. A URL is probed in code first, unless the caller already did
    (`probe`, passed on to the agent as context) or `skip_probe` sends it straight to the
    agent (re-discovering a source whose feed broke: the probe would find that feed again)."""
    kind, value = classify_input(query)
    if kind == "url":
        try:
            value = page_url(value)
        except ValueError as e:
            return DiscoveryOutcome(value, kind, [], "none", explanation=str(e))
        if probe is None and not skip_probe:
            if on_progress:
                on_progress(Progress("probe", value))
            try:
                probe = await probe_url(value, deps.http, now=deps.now())
            except HttpError as e:
                attempt = ProbeAttempt(url=value, outcome=str(e))
                probe = FeedProbe(input_url=value, candidates=[], sitemaps=[], attempts=[attempt])
        if probe is not None and probe.candidates:
            found = mark_duplicates(deps.conn, probe_candidates(probe))
            return DiscoveryOutcome(value, kind, found, "probe")
    if (reason := discovery_unavailable(deps)) is not None:
        return DiscoveryOutcome(value, kind, [], "none", explanation=reason)
    return await run_agent(value, kind, probe, deps, on_progress=on_progress)


def _explain(submission: Submission, stopped: str | None, count: int) -> str:
    parts: list[str] = []
    if stopped:
        parts.append(f"{stopped}; showing the {count} candidate(s) tested so far")
    elif not submission.submitted:
        parts.append(f"the agent finished without submitting; showing {count} tested")
    parts += submission.rejected
    if count == 0 and not parts:
        parts.append("no source could be tested successfully")
    return "; ".join(parts)


async def run_agent(
    query: str,
    kind: InputKind,
    probe: FeedProbe | None,
    deps: DiscoveryDeps,
    *,
    on_progress: Callable[[Progress], None] | None = None,
) -> DiscoveryOutcome:
    model = deps.resolver("smart", AGENT)
    runs = RunsRepo(deps.conn)
    repo = DiscoveryRepo(deps.conn)
    run_id = runs.start("discovery", now=deps.now())
    repo.start(run_id, query, kind, session_id=run_id, now=deps.now())
    ledger = UsageLedger(deps.conn, run_id, deps.config, model, deps.now)
    tested = VerifiedRecipes()
    toolkit = DiscoveryTools(deps.http, now=deps.now, tested=tested)
    guard = ToolGuard(on_progress=on_progress, clock=deps.clock or time.monotonic)
    sources = SourcesRepo(deps.conn)
    submission = Submission()

    def submit_candidates(
        candidates: list[CandidateArg], tool_context: ToolContext
    ) -> dict[str, Any]:
        """Your final answer: 1 to 5 candidates, each with a recipe test_recipe accepted.
        Call it once, at the end."""
        nonlocal submission
        submission = accept_submission(candidates, tested, sources.find_by_recipe_url)
        tool_context.actions.skip_summarization = True  # the final answer ends the run
        return {
            "ok": True,
            "accepted": len(submission.candidates),
            "rejected": submission.rejected,
        }

    tools: list[Any] = [*toolkit.functions(), submit_candidates]
    status: DiscoveryStatus = "ok"
    error: str | None = None
    try:
        provider = resolve_provider(deps.config.search, model)
        if provider == "gemini":
            tools.insert(0, google_search_tool(model, ledger))
        else:
            searcher = deps.searcher or make_searcher(provider, deps.config.search, deps.env)
            tools.insert(0, search_tool(searcher))
    except SearchUnavailable as e:
        error = str(e)  # the agent still has the probes and a URL to start from
    template = load_prompt("discovery")
    agent = LlmAgent(
        name="discovery",
        model=model.llm,
        instruction=lambda _ctx: template.system,
        tools=tools,
        # the turn cap first: a refused turn needs no budget check and spends nothing
        before_model_callback=[guard.before_model, ledger.before_model],
        after_model_callback=ledger.after_model,
        before_tool_callback=guard.before_tool,
        after_tool_callback=guard.after_tool,
    )
    sessions = SqliteSessionService(db_path=str(deps.sessions_db))
    runner = Runner(app=App(name=APP_NAME, root_agent=agent), session_service=sessions)
    stopped: str | None = None
    try:
        await sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=run_id)
        message = types.Content(
            role="user", parts=[types.Part.from_text(text=task_prompt(query, probe))]
        )
        async with asyncio.timeout(guard.max_seconds + HARD_STOP_GRACE_S):
            async for _ in runner.run_async(
                user_id=USER_ID, session_id=run_id, new_message=message
            ):
                pass
        stopped = guard.stopped
    except TimeoutError:
        stopped = f"stopped after {guard.max_seconds:.0f} s (the limit)"
    except Exception as exc:
        if is_budget_stop(exc):
            stopped = f"stopped: {describe(exc, 200)}"
        else:
            status, error = "failed", describe(exc, 300)
    except BaseException:  # cancelled (the user pressed Esc, or quit): never left 'running'
        runs.finish(run_id, "interrupted", now=deps.now(), stats={"tool_calls": guard.calls})
        repo.finish(
            run_id,
            "interrupted",
            tool_calls=guard.calls,
            candidates=[],
            explanation=None,
            now=deps.now(),
        )
        raise
    finally:
        await runner.close()
    candidates = submission.candidates
    if not candidates:  # stopped early, or nothing it submitted passed: offer what was tested
        candidates = fallback_candidates(tested, sources.find_by_recipe_url, query)
    if status != "failed" and (stopped or not submission.submitted):
        status = "partial"
    explanation = "; ".join(p for p in (_explain(submission, stopped, len(candidates)), error) if p)
    stats = {"tool_calls": guard.calls, "candidates": len(candidates)}
    runs.finish(run_id, status, now=deps.now(), error=error, stats=stats)
    repo.finish(
        run_id,
        status,
        tool_calls=guard.calls,
        candidates=[c.model_dump(mode="json") for c in candidates],
        explanation=explanation or None,
        now=deps.now(),
    )
    return DiscoveryOutcome(
        query,
        kind,
        candidates,
        "agent",
        run_id=run_id,
        explanation=explanation,
        status=status,
        tool_calls=guard.calls,
    )
