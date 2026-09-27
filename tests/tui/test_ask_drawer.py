import asyncio
from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from textual.widgets import Input, OptionList

from augury.agents.ask import REMOVED_WARNING, AskScope
from augury.core.config import Config, ScoutConfig, TuiConfig
from augury.core.db.asks_repo import AsksRepo
from augury.core.db.chunks_repo import ChunksRepo
from augury.core.db.runs_repo import RunsRepo
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.llm.resolver import ResolvedModel
from augury.rag.ingest import ingest_archive
from augury.tui.widgets.ask_drawer import AskDrawer
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.reader_pane import ReaderPane
from tests.helpers import CountingHttp, ScriptedLlm, fake_resolver
from tests.rag.helpers import add_items
from tests.tui.conftest import NOW, until
from tests.unit.test_extract_html import GENERIC

URL = "https://example.com/hf-blog/0-Head-pruning"
ONE = (("Head pruning", "prune attention heads"),)
TWO = (*ONE, ("Soup", "tomato"))  # the second is served at SOUP_URL
SOUP_URL = "https://example.com/hf-blog/1-Soup"
# A collapsed TL;DR box writes no TL;DR (P12), so every scripted reply goes to Ask.
NO_TLDR = Config(scout=ScoutConfig(auto_after_hours=0), tui=TuiConfig(tldr="collapsed"))


async def _app(make_app, replies, *, resolver=None, entries=ONE):
    embedder = HashEmbedder(64)
    llm = ScriptedLlm(replies=list(replies))
    app = make_app(
        config=NO_TLDR,
        embedder=embedder,
        resolver=resolver or fake_resolver(llm),
        http=CountingHttp({URL: GENERIC, SOUP_URL: GENERIC}),
    )
    [item, *_] = add_items(app.conn, entries, now=NOW)
    meter = EmbedMeter(app.conn, Config(), RunsRepo(app.conn).start("scout", now=NOW), lambda: NOW)
    await ingest_archive(app.conn, embedder=embedder, meter=meter, now=lambda: NOW)
    return app, item


async def _open(app, pilot, item):
    app.query_one(ItemsTable).select_key(item)
    await pilot.press("enter")
    await until(pilot, lambda: ChunksRepo(app.conn).for_item(item, "content") != [])


def _hanging(started: asyncio.Event):
    """A resolver whose smart model sets `started`, then waits on its reply for an hour."""

    class HangingLlm(BaseLlm):
        model: str = "hang"

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse]:
            started.set()
            await asyncio.sleep(3600)
            yield LlmResponse()

    hanging = ResolvedModel("fake/hang", "fake", False, HangingLlm())
    return lambda role, agent=None: hanging


def _ask_status(app) -> str | None:
    run = RunsRepo(app.conn).last("ask")
    return None if run is None else run.status


async def test_a_asks_about_the_open_item_and_shows_checked_citations(make_app):
    app, item = await _app(make_app, ["It is a post [1]. Also [5]. [bold red]x[/]"])
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(app, pilot, item)
        await pilot.press("a")
        drawer = app.query_one(AskDrawer)
        assert drawer.display and app.mode == "ASK"
        assert app.focused is app.query_one("#ask-input", Input)
        await pilot.press(*"what is it?", "enter")
        await until(pilot, lambda: "It is a post [1]" in drawer.answer_text)
        assert "[5]" not in drawer.answer_text
        assert "⚠ removed citation to unknown passage" in drawer.answer_text
        assert "[bold red]x[/]" in drawer.answer_text  # model text, shown literally
        assert app.query_one("#ask-sources", OptionList).option_count == 1
        [record] = AsksRepo(app.conn).recent()
        assert record.question == "what is it?" and record.scope["item_id"] == item
        await pilot.press("escape")
        assert not drawer.display and app.mode == "READ"


async def test_without_a_smart_model_a_explains_and_opens_nothing(make_app):
    app, _item = await _app(make_app, [], resolver=fake_resolver(unavailable="needs a key"))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await until(pilot, lambda: any("smart model" in n.message for n in app._notifications))
        assert not app.query_one(AskDrawer).display and app.mode == "NORMAL"


async def test_capital_a_asks_across_the_archive(make_app):
    app, item = await _app(make_app, ["Heads are pruned [1]."])
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(app, pilot, item)
        await pilot.press("A", *"pruning", "enter")
        drawer = app.query_one(AskDrawer)
        await until(pilot, lambda: "Heads are pruned [1]" in drawer.answer_text)
        assert AsksRepo(app.conn).recent()[0].scope == {"kind": "archive"}


async def test_quitting_while_ask_waits_on_the_model_is_clean(make_app):
    started = asyncio.Event()
    app, item = await _app(make_app, [], resolver=_hanging(started))
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(app, pilot, item)
        await pilot.press("a", *"why?", "enter")
        await until(pilot, lambda: started.is_set())
        await pilot.press("q")
    # The cancelled worker unwinds through ADK's root-node task: a few loop turns after quit.
    run = RunsRepo(app.conn).last("ask")
    for _ in range(500):
        if run is None or run.status != "running":
            break
        await asyncio.sleep(0)
        run = RunsRepo(app.conn).last("ask")
    assert run is not None and run.status == "interrupted"
    assert AsksRepo(app.conn).recent() == []


async def test_a_disguised_citation_never_reaches_the_screen(make_app):
    reply = "It is a post [1]. Hidden [7\u200b]. Nested [[7]9]. Range [1-9]."
    app, item = await _app(make_app, [reply])
    async with app.run_test(size=(160, 40)) as pilot:
        await _open(app, pilot, item)
        await pilot.press("a", *"what is it?", "enter")
        drawer = app.query_one(AskDrawer)
        await until(pilot, lambda: "It is a post [1]" in drawer.answer_text)
        [record] = AsksRepo(app.conn).recent()
        assert record.answer == "It is a post [1]. Hidden. Nested. Range."
        assert drawer.answer_text == f"{record.answer}\n{REMOVED_WARNING}"  # shown as stored


async def test_moving_to_another_item_closes_the_drawer_and_drops_its_ask(make_app):
    started = asyncio.Event()
    app, _ = await _app(make_app, [], resolver=_hanging(started), entries=TWO)
    async with app.run_test(size=(160, 40)) as pilot:
        first, second = list(app.query_one(ItemsTable).rows_by_key)
        await _open(app, pilot, first)
        await pilot.press("a", *"why?", "enter")
        await until(pilot, lambda: started.is_set())
        drawer = app.query_one(AskDrawer)
        app.query_one(ReaderPane).viewer.document.focus()  # back in the article, as a click does
        await pilot.press("n")
        assert app.reading_id == second and not drawer.display and app.mode == "READ"
        await until(pilot, lambda: _ask_status(app) == "interrupted")  # the answer on its way
        await pilot.press("a")  # a new question is about the item on screen
        assert drawer.display and drawer.scope == AskScope("item", item_id=second)
    assert AsksRepo(app.conn).recent() == []


async def test_an_archive_drawer_stays_across_items_and_leaving_the_reader_hides_it(make_app):
    app, _ = await _app(make_app, ["Heads are pruned [1]."], entries=TWO)
    async with app.run_test(size=(160, 40)) as pilot:
        first, second = list(app.query_one(ItemsTable).rows_by_key)
        await _open(app, pilot, first)
        await pilot.press("A", *"pruning", "enter")
        drawer = app.query_one(AskDrawer)
        await until(pilot, lambda: "Heads are pruned [1]" in drawer.answer_text)
        app.query_one(ReaderPane).viewer.document.focus()
        await pilot.press("n")  # the archive scope doesn't depend on the item
        assert app.reading_id == second and drawer.display
        assert drawer.scope == AskScope("archive")
        await pilot.press("escape")  # from the article: back to the table
        assert app.mode == "NORMAL" and not drawer.display
