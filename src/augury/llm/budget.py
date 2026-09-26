"""The daily budget (spec §5.9). A UsageLedger is attached to every LlmAgent: its
before_model_callback refuses the call once today's spend reaches a cap, and its
after_model_callback adds the call's tokens and cost to the run."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from google.adk.agents import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from augury.core.clock import local_day, local_day_bounds
from augury.core.config import BudgetConfig, Config
from augury.core.db.runs_repo import RunsRepo, Spend
from augury.llm.pricing import Price, price_for
from augury.llm.resolver import ResolvedModel


class BudgetExceeded(Exception):
    """Today's LLM budget is used up; AI features pause until tomorrow or a config change."""


def today_spend(conn: sqlite3.Connection, now: datetime) -> Spend:
    start, end = local_day_bounds(local_day(now))
    return RunsRepo(conn).spend_between(start, end)


def budget_problem(spend: Spend, budget: BudgetConfig) -> str | None:
    if spend.cost_usd >= budget.daily_usd:
        return (
            f"daily budget of ${budget.daily_usd:.2f} reached (${spend.cost_usd:.2f} spent today)"
        )
    if spend.unpriced_tokens >= budget.daily_tokens:
        return (
            f"daily cap of {budget.daily_tokens:,} tokens for models without a known price reached"
        )
    return None


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int


def usage_of(response: LlmResponse, *, native: bool) -> Usage:
    meta = response.usage_metadata
    if meta is None:
        return Usage(0, 0)
    tokens_out = meta.candidates_token_count or 0
    if native:  # Gemini reports thinking separately and bills it as output; LiteLLM folds it in
        tokens_out += meta.thoughts_token_count or 0
    return Usage(meta.prompt_token_count or 0, tokens_out)


@dataclass
class UsageLedger:
    conn: sqlite3.Connection
    run_id: str
    config: Config
    model: ResolvedModel
    now: Callable[[], datetime]
    price: Price | None = field(init=False)

    def __post_init__(self) -> None:
        self.price = price_for(self.model.spec, self.config, native=self.model.native)

    def before_model(
        self, callback_context: Context, llm_request: LlmRequest
    ) -> LlmResponse | None:
        # Raising here skips the model call; the caller sees BudgetExceeded as the node's error.
        today = today_spend(self.conn, self.now())
        if (problem := budget_problem(today, self.config.budget)) is not None:
            raise BudgetExceeded(problem)
        return None

    def after_model(
        self, callback_context: Context, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if llm_response.partial:  # a streamed fragment; the final response carries the totals
            return None
        usage = usage_of(llm_response, native=self.model.native)
        cost = self.price.cost(usage.tokens_in, usage.tokens_out) if self.price else 0.0
        RunsRepo(self.conn).add_usage(
            self.run_id,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=cost,
            unpriced_tokens=0 if self.price else usage.tokens_in + usage.tokens_out,
        )
        return None
