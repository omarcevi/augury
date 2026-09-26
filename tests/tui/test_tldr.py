"""F28: the TL;DR box in the reader. Made on Enter (or T), cached, and never on cursor movement."""

import asyncio
import json
import time

import pytest
from pydantic import PrivateAttr
from textual.content import Content as Visual
from textual.widgets import Static
from textual.worker import WorkerState

from augury.agents.normalize import store_items
from augury.core.db.contents_repo import ContentsRepo
from augury.core.db.state_repo import StateRepo
from augury.core.db.summaries_repo import SummariesRepo, Summary
from augury.core.models import Content, RawItem
from augury.extract.service import EXTRACTOR_VERSION
from augury.llm.prompt_registry import load_prompt
from augury.tui.keymap import KEYMAP
from augury.tui.ui_state import UiState, save
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.reader_pane import ReaderPane
from tests.helpers import CountingHttp, ScriptedLlm, fake_resolver, summary_reply
from tests.tui.conftest import NOW, until
from tests.tui.test_remember_state import config

BODY = "# Title\n\n" + "\n\n".join(f"## Part {i}\n\n" + "word " * 80 for i in range(4))


class GatedLlm(ScriptedLlm):
    """Replies (from `replies`) only once the test opens the gate, so it can act mid-TL;DR."""

    _gate: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def gate(self) -> asyncio.Event:
        return self._gate

    async def generate_content_async(self, llm_request, stream=False):
        await self._gate.wait()
        async for response in super().generate_content_async(llm_request, stream):
            yield response


def seed(app) -> list[str]:
    raws = [
        RawItem(
            source_id="hf-blog", url=f"https://x/{i}", title=f"Post {i}", summary=f"Summary {i}."
        )
        for i in range(3)
    ]
    ids = store_items(app.conn, raws, now=NOW).new_ids
    for item_id in ids:  # every body is cached, so Enter never needs the network
        ContentsRepo(app.conn).save(
            Content(
                item_id=item_id,
                status="ok",
                body_md=BODY,
                extractor="test",
                extractor_version=EXTRACTOR_VERSION,
                word_count=len(BODY.split()),
                fetched_at=NOW,
            )
        )
    return ids


def cache_tldr(app, item_id: str, first: str = "Cached point.") -> None:
    version = load_prompt("summarize").version
    SummariesRepo(app.conn).save(
        Summary(item_id, version, "fake/fake-model", [first, "b", "c"], ["x", "y", "z"], NOW)
    )


def busy(app) -> bool:
    return any(w.group in ("reader", "summary") and not w.is_finished for w in app.workers)


async def settle(pilot) -> None:
    """Both workers done (load_content starts load_summary) and the article at its position."""
    app = pilot.app
    await until(pilot, lambda: app.reading_id is not None and not busy(app) and app.reader_settled)


async def open_and_settle(pilot) -> None:
    # Pre-flight: make_app's debounce is `instant`; no fixed pauses (ledger rule).
    await pilot.press("enter")
    await settle(pilot)


def tldr(app) -> str:
    return app.query_one(ReaderPane).tldr_text


def summarize_runs(app) -> list[tuple[str, str | None]]:
    rows = app.conn.execute(
        "SELECT status, stats_json FROM runs WHERE kind = 'summarize' ORDER BY started_at, rowid"
    ).fetchall()
    return [(status, json.loads(stats).get("item_id")) for status, stats in rows]


async def test_cursor_movement_never_calls_the_model(make_app):
    llm, http = ScriptedLlm(), CountingHttp()  # no replies: any call is recorded, then fails
    app = make_app(http=http, resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "down", "up")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert llm.requests == [] and http.calls == []
        assert summarize_runs(app) == []


async def test_enter_makes_one_tldr_and_caches_it(make_app):
    llm = ScriptedLlm(replies=[summary_reply("A crisp first point.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert "A crisp first point." in tldr(app) and "Takeaways" in tldr(app)
        assert app.query_one("#reader-tldr", Static).display
        await pilot.press("escape")
        await open_and_settle(pilot)
        assert len(llm.requests) == 1  # cached by (item, prompt_version, model)
        assert "A crisp first point." in tldr(app)
        assert [status for status, _ in summarize_runs(app)] == ["ok"]


async def test_a_cached_tldr_shows_in_the_preview_without_a_call(make_app):
    llm = ScriptedLlm()
    app = make_app(resolver=fake_resolver(llm))
    for item_id in seed(app):
        cache_tldr(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await pilot.pause()
        assert "Cached point." in tldr(app) and llm.requests == []


async def test_moving_to_an_item_without_a_tldr_hides_the_box(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        first = app.query_one(ItemsTable).current_row()
        assert first is not None
        cache_tldr(app, first.id)
        await pilot.press("down", "up")
        await until(pilot, lambda: "Cached point." in tldr(app))
        await pilot.press("down")
        await until(pilot, lambda: tldr(app) == "")
        assert not app.query_one("#reader-tldr", Static).display


async def test_a_failed_tldr_offers_a_retry(make_app):
    llm = ScriptedLlm(replies=["bad", "bad", summary_reply("Third time lucky.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert "Press T to retry" in tldr(app)
        assert len(app.query("MarkdownH2")) == 4  # the article itself is there
        await pilot.press("T")
        await until(pilot, lambda: "Third time lucky." in tldr(app) and not busy(app))
        assert [status for status, _ in summarize_runs(app)] == ["failed", "ok"]


async def test_no_key_shows_no_tldr_box(make_app):
    app = make_app()  # the scrubbed environment has no key
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == "" and not app.query_one("#reader-tldr", Static).display
        assert len(app.query("MarkdownH2")) == 4  # the reader works as in M1
        await pilot.press("T")
        await until(pilot, lambda: bool(app._notifications))
        messages = [n.message for n in app._notifications]
        assert any("not configured" in m for m in messages), messages
        assert tldr(app) == "" and summarize_runs(app) == []


async def test_reopening_the_last_article_at_launch_never_calls_the_model(make_app, paths):
    # P2's reopen isn't an Enter (spec §8.3.1): a cached TL;DR shows, nothing new is made.
    llm = ScriptedLlm()
    app = make_app(config=config(reopen_last_article=True), resolver=fake_resolver(llm))
    ids = seed(app)
    save(paths, UiState(reading_item_id=ids[0], reading_progress=0.3))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        assert app.reading_id == ids[0] and tldr(app) == "" and llm.requests == []
    again = make_app(config=config(reopen_last_article=True), resolver=fake_resolver(llm))
    cache_tldr(again, ids[1])
    save(paths, UiState(reading_item_id=ids[1], reading_progress=0.3))
    async with again.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        assert again.reading_id == ids[1] and "Cached point." in tldr(again)
        assert llm.requests == [] and summarize_runs(again) == []


async def test_a_tldr_arriving_keeps_the_reading_position(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm(replies=[summary_reply()])))
    ids = seed(app)
    for item_id in ids:
        StateRepo(app.conn).set_progress(item_id, 0.5, now=NOW)
    async with app.run_test(size=(180, 30)) as pilot:
        await open_and_settle(pilot)
        assert "First point." in tldr(app)
        viewer = app.query_one(ReaderPane).viewer
        assert viewer.max_scroll_y > 0
        assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)


async def test_opening_another_item_interrupts_the_tldr_in_flight(make_app):
    llm = GatedLlm(replies=[summary_reply("For the second item.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await until(pilot, lambda: tldr(app) == "Summarizing…")
        first = app.reading_id
        await pilot.press("n")  # the next item's own TL;DR cancels this one
        await until(pilot, lambda: app.reading_id != first and tldr(app) == "Summarizing…")
        await until(pilot, lambda: [s for s, _ in summarize_runs(app)][:1] == ["interrupted"])
        llm.gate.set()
        await settle(pilot)
        assert "For the second item." in tldr(app)
        assert summarize_runs(app) == [("interrupted", first), ("ok", app.reading_id)]


async def test_t_while_summarizing_does_not_start_a_second_call(make_app):
    llm = GatedLlm(replies=[summary_reply("Only once.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await until(pilot, lambda: tldr(app) == "Summarizing…")
        await pilot.press("T")
        await pilot.pause()
        llm.gate.set()
        await settle(pilot)
        assert "Only once." in tldr(app)
        assert [status for status, _ in summarize_runs(app)] == ["ok"]


async def test_a_tldr_that_arrives_after_esc_still_replaces_summarizing(make_app):
    llm = GatedLlm(replies=[summary_reply("Worth the wait.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await until(pilot, lambda: tldr(app) == "Summarizing…")
        await pilot.press("escape")  # the article (and its box) stays on screen, wide
        llm.gate.set()
        await until(pilot, lambda: not busy(app))
        assert app.reading_id is None and "Worth the wait." in tldr(app)


async def test_quitting_mid_tldr_touches_no_widget(make_app):
    llm = GatedLlm()  # never replies
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await until(pilot, lambda: tldr(app) == "Summarizing…")
        [worker] = [w for w in app.workers if w.group == "summary"]
    # Quitting cancels the worker without awaiting it (asyncio.run does, in the real app).
    deadline = time.monotonic() + 5
    while not worker.is_finished and time.monotonic() < deadline:
        await asyncio.sleep(0)
    assert worker.state == WorkerState.CANCELLED  # not ERROR (e.g. a NoMatches after quit)
    assert [status for status, _ in summarize_runs(app)] == ["interrupted"]  # not 'running'


async def test_a_tldr_renders_literally(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    ids = seed(app)
    version = load_prompt("summarize").version
    for item_id in ids:
        SummariesRepo(app.conn).save(
            Summary(
                item_id,
                version,
                "fake/fake-model",
                ["[bold red]loud[/] \x1b[31mred", "[link=file:///etc/passwd]x[/link]", "c"],
                ["[i]x[/i]", "y", "z"],
                NOW,
            )
        )
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        box = app.query_one("#reader-tldr", Static)
        visual = box.visual
        assert isinstance(visual, Visual)
        shown = visual.plain
        assert "[bold red]loud[/]" in shown and "[link=file:///etc/passwd]x[/link]" in shown
        assert "[i]x[/i]" in shown and "\x1b" not in shown


LONG_TLDR = [
    "Speculative decoding drafts several tokens with a small model and verifies them in one pass"
    " of the large model, cutting latency without changing the output distribution at all.",
    "The new method adapts the draft length per request from the acceptance rate, which keeps"
    " the speedup when the draft model and the target model disagree more often.",
    "It matters because serving costs are dominated by memory-bound decoding, and this gives up"
    " to 2.8x faster generation on the same hardware with no retraining.",
]
LONG_TAKEAWAYS = [
    "Draft lengths of 4 to 6 tokens gave the best trade-off in their benchmarks.",
    "Acceptance rates below 60% erase most of the gain; measure yours before adopting it.",
    "Works with any target model that exposes logits; no fine-tuning is needed.",
    "Batching many requests reduces the benefit because decoding becomes compute-bound.",
    "Code and configs are released under Apache 2.0, see Zebrafive.",
]


async def test_a_long_tldr_scrolls_inside_a_box_of_at_most_half_the_reader(make_app):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    version = load_prompt("summarize").version
    for item_id in seed(app):
        SummariesRepo(app.conn).save(
            Summary(item_id, version, "fake/fake-model", LONG_TLDR, LONG_TAKEAWAYS, NOW)
        )
    async with app.run_test(size=(130, 40)) as pilot:
        await open_and_settle(pilot)
        reader = app.query_one(ReaderPane)
        box, body = app.query_one("#reader-tldr-box"), app.query_one("#reader-tldr", Static)
        assert box.display and body.display
        # At most half the reader pane (an overflowing TL;DR fills it), the article keeps the rest.
        assert box.outer_size.height == reader.content_size.height // 2 == 15
        assert reader.viewer.size.height >= reader.content_size.height / 3
        # Nothing is dropped: the whole TL;DR is the box's scrollable content, all 5 takeaways.
        assert all(point in tldr(app) for point in LONG_TAKEAWAYS)
        lines = body.visual.get_height(body.styles, body.content_size.width)  # all of it, wrapped
        assert lines > box.content_size.height  # more than the box shows at once
        assert body.content_size.height == lines  # the text itself isn't cut
        assert box.virtual_size.height >= body.outer_size.height  # and all of it scrolls
        assert box.max_scroll_y > 0 and box.show_vertical_scrollbar  # it overflows: a scrollbar
        assert "Zebrafive" not in app.export_screenshot()  # the last takeaway, below the fold
        # From the keyboard: shift+tab from the article reaches the box, end scrolls it there.
        await pilot.press("shift+tab")
        assert app.focused is box
        await pilot.press("end")
        await until(pilot, lambda: box.scroll_y == box.max_scroll_y)
        assert "Zebrafive" in app.export_screenshot()
        await pilot.press("j")  # the reader's keys still scroll the article
        await until(pilot, lambda: reader.viewer.scroll_y > 0)
        # A new item's TL;DR starts at its top.
        await pilot.press("n")
        await settle(pilot)
        assert box.scroll_y == 0 and app.focused is reader.viewer.document


async def test_t_is_in_the_help_but_not_in_the_hints_row(make_app):
    assert "T" not in {hint.key for hint in KEYMAP["READ"]}  # the READ row is as before F28
    app = make_app()
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpOverlay))
        body = str(app.screen.query_one("#help-text").render())
        assert "T          retry TL;DR" in body
