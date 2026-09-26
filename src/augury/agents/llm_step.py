"""How every LLM call in augury runs: an LlmAgent built by make_agent (budget gate and usage
ledger attached), called from a code node through ctx.run_node, so it is a node of the same
Workflow graph (spec §3.2). Callers classify failures with is_schema_failure/is_budget_stop."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from google.adk.agents import Context, LlmAgent
from google.adk.agents.llm_agent import ToolUnion
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, ValidationError

from augury.llm.budget import BudgetExceeded, UsageLedger
from augury.llm.prompt_registry import PromptTemplate
from augury.llm.resolver import ResolvedModel

APP_NAME = "augury"
# ADK logs every failing node with a traceback. Failures are recorded by us (runs.error, the
# TUI), and a traceback on stderr would corrupt the TUI, so ADK's logger stays quiet.
logging.getLogger("google_adk").addHandler(logging.NullHandler())

Call = Callable[[str], Awaitable[Any]]


class SchemaFailure(Exception):
    """A reply parsed but broke a rule the schema can't express (e.g. an idx is missing)."""


def unwrap(exc: BaseException) -> BaseException:
    # ctx.run_node wraps a child node's exception in DynamicNodeFailError and keeps it in .error.
    inner = getattr(exc, "error", None)
    return inner if isinstance(inner, BaseException) else exc


def is_schema_failure(exc: BaseException) -> bool:
    return isinstance(unwrap(exc), ValidationError | SchemaFailure)


def is_budget_stop(exc: BaseException) -> bool:
    return isinstance(unwrap(exc), BudgetExceeded)


def describe(exc: BaseException, limit: int = 1000) -> str:
    inner = unwrap(exc)
    return f"{type(inner).__name__}: {inner}"[:limit]


def make_agent(
    name: str,
    template: PromptTemplate,
    model: ResolvedModel,
    *,
    ledger: UsageLedger | None = None,
    output_schema: type[BaseModel] | None = None,
    tools: Sequence[ToolUnion] = (),
) -> LlmAgent:
    system = template.system
    return LlmAgent(
        name=name,
        model=model.llm,
        # A callable is passed through untouched. A plain str would get its {placeholders}
        # filled from session state, and an unknown one raises KeyError.
        instruction=lambda _ctx: system,
        output_schema=output_schema,
        tools=list(tools),
        before_model_callback=ledger.before_model if ledger else None,
        after_model_callback=ledger.after_model if ledger else None,
    )


def node_caller(ctx: Context, agent: LlmAgent) -> Call:
    """Each call runs `agent` as a fresh child node of the calling node (which must be
    @node(rerun_on_resume=True)). With an output_schema the result is the validated dict."""

    async def call(text: str) -> Any:
        return await ctx.run_node(agent, node_input=text)

    return call


async def run_workflow(workflow: Workflow, text: str = "run") -> None:
    runner = InMemoryRunner(app=App(name=APP_NAME, root_agent=workflow))
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id="local")
    message = types.Content(role="user", parts=[types.Part.from_text(text=text)])
    async for _ in runner.run_async(user_id="local", session_id=session.id, new_message=message):
        pass


async def run_in_node[T](name: str, body: Callable[[Context], Awaitable[T]]) -> T:
    """Run `body` as the only node of a one-node Workflow, so the agents it calls through
    node_caller are graph nodes too (for LLM work outside the scout: TL;DR on Enter, probes)."""
    results: list[T] = []
    errors: list[Exception] = []

    @node(name=name, rerun_on_resume=True)
    async def only(ctx: Context, node_input: types.Content) -> None:
        try:
            results.append(await body(ctx))
        except Exception as exc:  # re-raised below with its own type, whatever ADK does with it
            errors.append(exc)

    await run_workflow(Workflow(name=name, edges=[("START", only)]), name)
    if errors:
        raise errors[0]
    if not results:
        raise RuntimeError(f"workflow {name!r} finished without a result")
    return results[0]
