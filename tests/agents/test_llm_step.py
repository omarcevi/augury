from datetime import UTC, datetime

from pydantic import BaseModel

from augury.agents.llm_step import (
    describe,
    is_budget_stop,
    is_schema_failure,
    make_agent,
    node_caller,
    run_in_node,
)
from augury.core.config import BudgetConfig, Config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.llm.budget import UsageLedger, today_spend
from augury.llm.prompt_registry import PromptTemplate
from augury.llm.resolver import ResolvedModel
from tests.helpers import ScriptedLlm

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TEMPLATE = PromptTemplate("test", 1, 'Reply as JSON like {"word": "..."} for {idx}.')


class Answer(BaseModel):
    word: str


def agent_with(paths, llm: ScriptedLlm, config: Config | None = None):
    conn = open_db(paths, now=NOW)
    run_id = RunsRepo(conn).start("summarize", now=NOW)
    model = ResolvedModel(spec="fake/fake-model", provider="fake", native=False, llm=llm)
    ledger = UsageLedger(conn, run_id, config or Config(), model, lambda: NOW)
    return conn, make_agent("test_llm", TEMPLATE, model, ledger=ledger, output_schema=Answer)


async def call_once(agent, text: str = "hello") -> object:
    async def body(ctx) -> object:
        try:
            return await node_caller(ctx, agent)(text)
        except Exception as exc:  # returned, so the test can look at it
            return exc

    return await run_in_node("llm_step_test", body)


async def test_the_instruction_reaches_the_model_verbatim(paths):
    llm = ScriptedLlm(replies=['{"word": "hi"}'])
    _conn, agent = agent_with(paths, llm)
    assert await call_once(agent) == {"word": "hi"}
    assert llm.requests[0].config.system_instruction == TEMPLATE.system  # {idx} not templated
    assert llm.prompts == ["hello"]


async def test_invalid_json_is_a_schema_failure(paths):
    _conn, agent = agent_with(paths, ScriptedLlm(replies=["not json"]))
    result = await call_once(agent)
    assert isinstance(result, Exception) and is_schema_failure(result)
    assert describe(result).startswith("ValidationError")


async def test_provider_errors_are_not_schema_failures(paths):
    _conn, agent = agent_with(paths, ScriptedLlm(replies=[RuntimeError("503 unavailable")]))
    result = await call_once(agent)
    assert isinstance(result, Exception)
    assert not is_schema_failure(result) and not is_budget_stop(result)
    assert "503 unavailable" in describe(result)


async def test_each_call_is_recorded_on_the_run(paths):
    conn, agent = agent_with(paths, ScriptedLlm(replies=['{"word": "hi"}']))
    await call_once(agent)
    spend = today_spend(conn, NOW)
    assert (spend.tokens_in, spend.tokens_out) == (100, 20)


async def test_a_spent_budget_stops_the_call_before_the_model(paths):
    llm = ScriptedLlm(replies=['{"word": "hi"}'])
    _conn, agent = agent_with(paths, llm, Config(budget=BudgetConfig(daily_usd=0)))
    result = await call_once(agent)
    assert isinstance(result, Exception) and is_budget_stop(result)
    assert llm.requests == []
