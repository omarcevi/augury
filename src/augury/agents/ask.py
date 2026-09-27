"""Ask (spec §5.7): a question about one item or the archive, answered with cited passages.
Retrieval is code: hybrid search, top 8, filters inside the KNN; an item not chunked yet is
extracted and ingested first. Then one call to the smart model, as a standalone agent run
through its own Runner (the island pattern, spec §3.2), with the numbered passages as fenced
data. Citations are checked in code: only [n] for a retrieved passage survive, the others are
removed and the answer says so. Every question is saved in `asks`, its spend on an `ask` run."""

import re
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from augury.agents.llm_step import describe
from augury.core.clock import day_number, local_day
from augury.core.config import Config
from augury.core.db.asks_repo import AsksRepo
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.state_repo import StateRepo
from augury.core.text import strip_control_chars
from augury.extract.service import get_or_extract
from augury.llm.budget import UsageLedger, budget_problem, today_spend
from augury.llm.embedder import Embedder, EmbedMeter
from augury.llm.prompt_registry import load_prompt
from augury.llm.resolver import ModelUnavailable, ResolvedModel, Resolver
from augury.llm.safe_prompt import prompt
from augury.rag.guard import vector_problem
from augury.rag.ingest import ingest_content
from augury.rag.search import SearchFilters, search
from augury.sources.http import HttpClient

AGENT = "ask"
APP_NAME = "augury-ask"
USER_ID = "local"
TOP_PASSAGES = 8
MAX_QUESTION_CHARS = 2000
REMOVED_WARNING = "⚠ removed citation to unknown passage"
UNCITED_WARNING = "⚠ this answer cites no passage"
NO_PASSAGES = "No passage in scope matches this question."
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")  # an innermost bracket group
_CITATION = re.compile(r"\s*([0-9]{1,4}(?:\s*,\s*[0-9]{1,4})*)\s*")  # its inside: n or n, m
_HELD = re.compile(r"\x00([a-j, ]+)\x01")  # a checked citation, held out of later passes
_TO_LETTERS = str.maketrans("0123456789", "abcdefghij")
_TO_DIGITS = str.maketrans("abcdefghij", "0123456789")
# Lookalike brackets (fullwidth, lenticular, tortoise shell) are read, and shown, as [ ].
_LOOKALIKES = str.maketrans("\uff3b\u3010\u3014\u3016\uff3d\u3011\u3015\u3017", "[[[[]]]]")
_SPACE_BEFORE_MARK = re.compile(r"[ \t]+([.,;:!?])")


class AskUnavailable(Exception):
    """Ask can't run now (spec §11): no smart model, no embedder or index, or the budget."""


@dataclass(frozen=True)
class AskScope:
    kind: Literal["item", "archive"]
    item_id: str | None = None
    source_id: str | None = None
    days: int | None = None  # the last N days (published), for the archive

    def filters(self, today: datetime) -> SearchFilters:
        day_from = None
        if self.days is not None:
            day_from = day_number(local_day(today) - timedelta(days=self.days))
        return SearchFilters(
            collections=("content", "archive"),
            item_id=self.item_id if self.kind == "item" else None,
            source_ids=frozenset({self.source_id}) if self.source_id else frozenset(),
            day_from=day_from,
        )

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class Passage:
    n: int  # the number the model cites, 1-based
    chunk_id: int
    item_id: str
    title: str
    source_id: str
    section: str
    header: str
    text: str


@dataclass
class AskAnswer:
    question: str
    answer: str  # as shown: invalid citations already removed
    passages: list[Passage]
    cited: list[int] = field(default_factory=list)  # valid [n], in order of first use
    removed: list[int | str] = field(default_factory=list)  # [n] made up, or [2-9] as written
    warning: str = ""
    ask_id: int | None = None
    run_id: str | None = None


@dataclass
class AskDeps:
    conn: sqlite3.Connection
    http: HttpClient
    config: Config
    resolver: Resolver
    embedder: Embedder | None
    now: Callable[[], datetime]
    unavailable: str | None = None  # why there is no embedder


def _sanitized(text: str) -> str:
    """The reply as the screen shows it (no control characters or ANSI), without invisible
    format characters (zero-width space and joiner, BOM, bidi controls) and with lookalike
    brackets made plain: citations are checked on exactly the text that is shown and stored."""
    shown = strip_control_chars(text).translate(_LOOKALIKES)
    return "".join(c for c in shown if unicodedata.category(c) != "Cf")


def _restore(held: re.Match[str]) -> str:
    return f"[{held.group(1).translate(_TO_DIGITS)}]"


def check_citations(text: str, count: int) -> tuple[str, list[int], list[int | str]]:
    """(answer with only valid citations, cited, removed), on the sanitized reply: the answer
    is what is shown and stored. A citation is [n] or [n, m, ...] of 1-4 digit numbers, and a
    group like [1, 9] keeps [1]. Any other bracket group holding a digit ([2-9], [p7], [2024])
    is removed whole. Passes repeat until nothing changes, so a removal can't assemble a new
    citation ([[7]9] -> [9] -> gone); checked citations are held out of the later passes."""
    answer = _sanitized(text)
    removed: list[int | str] = []

    def note(entry: int | str) -> None:
        if entry not in removed:
            removed.append(entry)

    def check(group: re.Match[str]) -> str:
        inside = group.group(1)
        if not any(c.isnumeric() for c in inside):
            return group.group(0)  # [the method] is not a citation
        if (strict := _CITATION.fullmatch(inside)) is None:
            note(_HELD.sub(_restore, group.group(0))[:40])
            return ""
        numbers = [int(n) for n in re.split(r"\s*,\s*", strict.group(1))]
        valid = [n for n in dict.fromkeys(numbers) if 1 <= n <= count]
        for n in numbers:
            if n not in valid:
                note(n)
        return f"\x00{', '.join(map(str, valid)).translate(_TO_LETTERS)}\x01" if valid else ""

    while (checked := _BRACKETS.sub(check, answer)) != answer:
        answer = checked
    held = (h.translate(_TO_DIGITS) for h in _HELD.findall(answer))
    cited = list(dict.fromkeys(int(n) for h in held for n in h.split(", ")))
    answer = _HELD.sub(_restore, answer)
    answer = _SPACE_BEFORE_MARK.sub(r"\1", re.sub(r"[ \t]{2,}", " ", answer)).strip()
    return answer, cited, removed


def ask_unavailable(deps: AskDeps) -> str | None:
    try:
        deps.resolver("smart", AGENT)
    except ModelUnavailable as e:
        return f"Ask needs the smart model: {e}"
    if (problem := vector_problem(deps.conn, deps.embedder, deps.unavailable)) is not None:
        return f"Ask needs the search index: {problem}"
    spend = today_spend(deps.conn, deps.now())
    if (problem := budget_problem(spend, deps.config.budget)) is not None:
        return f"Ask is paused: {problem}"
    return None


def ask_prompt(question: str, passages: Sequence[Passage]) -> str:
    numbered = "\n\n".join(f"[{p.n}] {p.header}\n{p.text}" for p in passages)
    return prompt(t"Question:\n{question}\n\nPassages:\n{numbered}")


async def call_model(model: ResolvedModel, ledger: UsageLedger, text: str) -> str:
    """One model turn in a standalone agent with its own Runner; its final text."""
    template = load_prompt("ask")
    agent = LlmAgent(
        name="ask",
        model=model.llm,
        instruction=lambda _ctx: template.system,  # a callable: never filled from state
        before_model_callback=ledger.before_model,
        after_model_callback=ledger.after_model,
    )
    runner = Runner(
        app=App(name=APP_NAME, root_agent=agent), session_service=InMemorySessionService()
    )
    reply = ""
    try:
        session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        message = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session.id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text or "" for p in event.content.parts if not p.thought)
    finally:
        await runner.close()
    return reply


async def _ensure_content(deps: AskDeps, item_id: str, meter: EmbedMeter) -> None:
    """Spec §5.7: an item that has no content passages yet is ingested on demand."""
    if ChunksRepo(deps.conn).for_item(item_id, "content"):
        return
    item = ItemsRepo(deps.conn).get(item_id)
    if item is None:
        return
    content = await get_or_extract(deps.conn, deps.http, item, now=deps.now())
    await ingest_content(
        deps.conn, item, content, embedder=deps.embedder, meter=meter, now=deps.now
    )


def _passages(conn: sqlite3.Connection, hits: Sequence[Any]) -> list[Passage]:
    ids = list(dict.fromkeys(h.chunk.item_id for h in hits))
    titles: dict[str, str] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        titles = dict(conn.execute(f"SELECT id, title FROM items WHERE id IN ({marks})", ids))
    return [
        Passage(
            n=n,
            chunk_id=h.chunk.id,
            item_id=h.chunk.item_id,
            title=titles.get(h.chunk.item_id, ""),
            source_id=h.chunk.source_id,
            section=h.chunk.section,
            header=h.chunk.context_header,
            text=h.chunk.text,
        )
        for n, h in enumerate(hits, 1)
    ]


async def ask(question: str, scope: AskScope, deps: AskDeps) -> AskAnswer:
    question = " ".join(strip_control_chars(question).split())[:MAX_QUESTION_CHARS]
    if not question:
        raise ValueError("the question is empty")
    if scope.kind == "item" and scope.item_id is None:
        raise ValueError("an item question needs the item")
    if (reason := ask_unavailable(deps)) is not None:
        raise AskUnavailable(reason)
    model = deps.resolver("smart", AGENT)
    runs = RunsRepo(deps.conn)
    run_id = runs.start("ask", now=deps.now())
    meter = EmbedMeter(deps.conn, deps.config, run_id, deps.now)
    try:
        if scope.kind == "item" and scope.item_id is not None:
            await _ensure_content(deps, scope.item_id, meter)
        hits = await search(
            deps.conn,
            question,
            scope.filters(deps.now()),
            embedder=deps.embedder,
            meter=meter,
            top_k=TOP_PASSAGES,
            unavailable=deps.unavailable,
        )
        passages = _passages(deps.conn, hits)
        if not passages:  # nothing to answer from: no model call, nothing spent on one
            result = AskAnswer(question, NO_PASSAGES, [])
        else:
            ledger = UsageLedger(deps.conn, run_id, deps.config, model, deps.now)
            reply = await call_model(model, ledger, ask_prompt(question, passages))
            answer, cited, removed = check_citations(reply, len(passages))
            warnings = [REMOVED_WARNING] if removed else []
            if not cited and answer:
                warnings.append(UNCITED_WARNING)
            result = AskAnswer(question, answer, passages, cited, removed, " · ".join(warnings))
    except BaseException as exc:  # includes cancellation: never leave a 'running' row
        failed = isinstance(exc, Exception)
        runs.finish(
            run_id,
            "failed" if failed else "interrupted",
            now=deps.now(),
            error=describe(exc, 300) if failed else None,
        )
        raise
    by_n = {p.n: p for p in result.passages}
    result.run_id = run_id
    result.ask_id = AsksRepo(deps.conn).save(
        run_id=run_id,
        scope=scope.to_json(),
        question=question,
        answer=result.answer,
        citations=[
            {"n": n, "chunk_id": by_n[n].chunk_id, "item_id": by_n[n].item_id} for n in result.cited
        ],
        model=model.spec,
        now=deps.now(),
    )
    if scope.item_id is not None:
        StateRepo(deps.conn).log(scope.item_id, "ask", now=deps.now())
    runs.finish(
        run_id,
        "ok",
        now=deps.now(),
        stats={"passages": len(result.passages), "cited": result.cited, "removed": result.removed},
    )
    return result
