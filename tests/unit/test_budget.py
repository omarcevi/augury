from datetime import UTC, datetime, timedelta

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from augury.core.config import BudgetConfig, Config, PriceConfig, load_config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo, Spend
from augury.llm.budget import BudgetExceeded, UsageLedger, budget_problem, today_spend
from augury.llm.resolver import ResolvedModel
from tests.helpers import ScriptedLlm

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
FAKE = "fake/fake-model"
PRICED = Config(pricing={FAKE: PriceConfig(input_per_mtok=1.0, output_per_mtok=10.0)})


def response(prompt: int = 1000, out: int = 100, thoughts: int | None = None) -> LlmResponse:
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts
    )
    return LlmResponse(usage_metadata=usage)


def ledger(paths, config: Config, *, native: bool = False) -> tuple[UsageLedger, str]:
    conn = open_db(paths, now=NOW)
    run_id = RunsRepo(conn).start("scout", now=NOW)
    model = ResolvedModel(spec=FAKE, provider="fake", native=native, llm=ScriptedLlm())
    return UsageLedger(conn, run_id, config, model, lambda: NOW), run_id


def test_usage_and_cost_land_on_the_run(paths):
    led, run_id = ledger(paths, PRICED)
    led.after_model(None, response())  # type: ignore[arg-type]
    spend = today_spend(led.conn, NOW)
    assert (spend.tokens_in, spend.tokens_out, spend.unpriced_tokens) == (1000, 100, 0)
    assert spend.cost_usd == pytest.approx(0.001 + 0.001)
    row = led.conn.execute("SELECT tokens_in, cost_usd FROM runs WHERE id = ?", (run_id,))
    assert tuple(row.fetchone()) == (1000, pytest.approx(0.002))


def test_gemini_thinking_is_billed_as_output(paths):
    led, _ = ledger(paths, PRICED, native=True)
    led.after_model(None, response(out=100, thoughts=400))  # type: ignore[arg-type]
    assert today_spend(led.conn, NOW).tokens_out == 500


def test_litellm_reasoning_is_not_counted_twice(paths):
    led, _ = ledger(paths, PRICED, native=False)  # completion_tokens already include reasoning
    led.after_model(None, response(out=500, thoughts=400))  # type: ignore[arg-type]
    assert today_spend(led.conn, NOW).tokens_out == 500


def test_tokens_from_a_model_without_a_price_are_unpriced(paths):
    led, _ = ledger(paths, Config())
    led.after_model(None, response())  # type: ignore[arg-type]
    spend = today_spend(led.conn, NOW)
    assert spend.cost_usd == 0 and spend.unpriced_tokens == 1100


def test_the_gate_is_open_until_the_budget_is_spent(paths):
    led, run_id = ledger(paths, PRICED)
    assert led.before_model(None, None) is None  # type: ignore[arg-type]
    RunsRepo(led.conn).add_usage(run_id, tokens_in=0, tokens_out=0, cost_usd=1.0, unpriced_tokens=0)
    with pytest.raises(BudgetExceeded, match=r"\$1\.00"):
        led.before_model(None, None)  # type: ignore[arg-type]


def test_budget_problem_covers_both_caps():
    budget = BudgetConfig(daily_usd=1.0, daily_tokens=1000)
    assert budget_problem(Spend(cost_usd=0.5, unpriced_tokens=10), budget) is None
    assert "$1.00" in (budget_problem(Spend(cost_usd=1.0), budget) or "")
    assert "1,000 tokens" in (budget_problem(Spend(unpriced_tokens=1000), budget) or "")


def test_spend_is_counted_per_local_day(paths):
    conn = open_db(paths, now=NOW)
    runs = RunsRepo(conn)
    old = runs.start("summarize", now=NOW - timedelta(days=2))
    runs.add_usage(old, tokens_in=5, tokens_out=5, cost_usd=0.9, unpriced_tokens=0)
    new = runs.start("summarize", now=NOW)
    runs.add_usage(new, tokens_in=1, tokens_out=1, cost_usd=0.1, unpriced_tokens=0)
    assert today_spend(conn, NOW).cost_usd == pytest.approx(0.1)


def test_budget_and_pricing_config_sections(paths):
    paths.config_file.write_text(
        '[budget]\ndaily_usd = 0.5\n\n[pricing."openai/some-model"]\n'
        "input_per_mtok = 1.5\noutput_per_mtok = 6\n"
    )
    config = load_config(paths)
    assert config.budget.daily_usd == 0.5 and config.budget.daily_tokens == 2_000_000
    assert config.pricing["openai/some-model"].output_per_mtok == 6
