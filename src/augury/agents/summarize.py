"""TL;DRs (spec §5.5). The title and full text (cut to [summarizer] max_input_chars) go to the
fast model as fenced data; the reply is 3 TL;DR bullets of at most 30 words plus 3 to 5
takeaways. Cached by (item, prompt_version, model), and made only on Enter or by the scout's
prefetch, never on cursor movement."""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from augury.agents.llm_step import (
    Call,
    SchemaFailure,
    describe,
    is_schema_failure,
    make_agent,
    node_caller,
    run_in_node,
)
from augury.core.config import Config
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.summaries_repo import SummariesRepo, Summary
from augury.core.models import Content, Item
from augury.core.text import strip_control_chars
from augury.llm.budget import UsageLedger, budget_problem, today_spend
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import ModelUnavailable, Resolver, model_spec
from augury.llm.safe_prompt import prompt

AGENT = "summarizer"
TLDR_BULLETS = 3
TLDR_WORDS = 30
MIN_TAKEAWAYS, MAX_TAKEAWAYS = 3, 5


class SummaryReply(BaseModel):
    """The model's reply; also the LlmAgent's output_schema."""

    tldr: list[str]
    takeaways: list[str]


def _clean(text: str) -> str:
    return " ".join(strip_control_chars(text).split()).lstrip("•*-· ").strip()


def _words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + "…"


def accept_summary(output: object) -> tuple[list[str], list[str]]:
    if output is None:
        raise SchemaFailure("the reply was empty")
    reply = SummaryReply.model_validate(output)
    tldr = [_words(b, TLDR_WORDS) for b in map(_clean, reply.tldr) if b][:TLDR_BULLETS]
    takeaways = [t for t in map(_clean, reply.takeaways) if t][:MAX_TAKEAWAYS]
    if len(tldr) < TLDR_BULLETS:
        raise SchemaFailure(f"tldr needs {TLDR_BULLETS} bullets, got {len(tldr)}")
    if len(takeaways) < MIN_TAKEAWAYS:
        raise SchemaFailure(
            f"takeaways needs {MIN_TAKEAWAYS} to {MAX_TAKEAWAYS} bullets, got {len(takeaways)}"
        )
    return tldr, takeaways


def summary_prompt(item: Item, body_md: str, max_chars: int) -> str:
    title, body = item.title, body_md[:max_chars]
    return prompt(t"Title:\n{title}\n\nFull text:\n{body}")


def repair_prompt(original: str, error: str) -> str:
    # The error quotes the rejected reply, which can echo the article, so it is data too.
    return prompt(
        t"{original:trusted}\n\nYour previous reply was rejected because:\n{error}\n"
        t"Reply again with valid JSON only."
    )


async def summarize_with(
    call: Call, item: Item, body_md: str, *, max_chars: int
) -> tuple[list[str], list[str]]:
    """One attempt and, after a schema failure, one repair (spec §11). Other errors propagate."""
    text = summary_prompt(item, body_md, max_chars)
    try:
        return accept_summary(await call(text))
    except Exception as exc:
        if not is_schema_failure(exc):
            raise
        error = describe(exc)
    return accept_summary(await call(repair_prompt(text, error)))


class Summarizer:
    """TL;DRs for one app or one scout: the model, the cache key and the budget."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        resolver: Resolver,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self.conn, self.config, self.resolver, self.now = conn, config, resolver, now
        try:
            self.model_spec = resolver("fast", AGENT).spec
            self.available = True
        except ModelUnavailable:
            self.model_spec = model_spec(config, "fast", AGENT)
            self.available = False

    def cached(self, item_id: str) -> Summary | None:
        """Database only, so it is safe on every cursor move."""
        version = load_prompt("summarize").version
        return SummariesRepo(self.conn).get(item_id, prompt_version=version, model=self.model_spec)

    def budget_problem(self) -> str | None:
        return budget_problem(today_spend(self.conn, self.now()), self.config.budget)

    async def summarize(
        self,
        item: Item,
        content: Content,
        *,
        run_id: str | None = None,
        make_call: Callable[[LlmAgent], Call] | None = None,
    ) -> Summary:
        """The cached TL;DR, or a new one. Usage goes to `run_id` (the scout's prefetch, which
        passes its node's `make_call`) or to a `summarize` run of its own. Raises
        ModelUnavailable, the budget stop, or the model's error."""
        if (hit := self.cached(item.id)) is not None:
            return hit
        model = self.resolver("fast", AGENT)
        template = load_prompt("summarize")
        runs = RunsRepo(self.conn)
        own_run = run_id is None
        rid = run_id if run_id is not None else runs.start("summarize", now=self.now())
        ledger = UsageLedger(self.conn, rid, self.config, model, self.now)
        agent = make_agent(
            "summarizer_llm", template, model, ledger=ledger, output_schema=SummaryReply
        )
        limit = self.config.summarizer.max_input_chars
        try:
            if make_call is not None:
                tldr, takeaways = await summarize_with(
                    make_call(agent), item, content.body_md, max_chars=limit
                )
            else:
                tldr, takeaways = await run_in_node(
                    "summarize",
                    lambda ctx: summarize_with(
                        node_caller(ctx, agent), item, content.body_md, max_chars=limit
                    ),
                )
        except BaseException as exc:  # includes cancellation: never leave a 'running' row
            if own_run:
                status = "failed" if isinstance(exc, Exception) else "interrupted"
                runs.finish(
                    rid,
                    status,
                    now=self.now(),
                    error=describe(exc, 300),
                    stats={"item_id": item.id},
                )
            raise
        summary = Summary(item.id, template.version, model.spec, tldr, takeaways, self.now())
        SummariesRepo(self.conn).save(summary)
        if own_run:
            runs.finish(rid, "ok", now=self.now(), stats={"item_id": item.id})
        return summary
