"""P12: h collapses the reader's TL;DR box to one line and back. Collapsed writes no TL;DRs."""

import pytest
from rich.cells import cell_len
from textual.widgets import Static

from augury.core.db.runs_repo import RunsRepo
from augury.core.db.state_repo import StateRepo
from augury.tui.keymap import KEYMAP
from augury.tui.ui_state import UiState, load, save
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.reader_pane import ReaderPane
from tests.helpers import ScriptedLlm, fake_resolver, summary_reply
from tests.tui.conftest import NOW, until
from tests.tui.test_remember_state import config
from tests.tui.test_tldr import (
    GatedLlm,
    busy,
    cache_tldr,
    open_and_settle,
    seed,
    settle,
    summarize_runs,
    tldr,
)

COLLAPSED = "TL;DR ▸  h to show"
NOT_WRITTEN = "TL;DR ▸  not written yet · h to show"
SUMMARIZING = "TL;DR ▸  summarizing… · h to show"


def collapsed(app) -> bool:
    return app.query_one(ReaderPane).tldr_collapsed


def position(app) -> float:
    viewer = app.query_one(ReaderPane).viewer
    assert viewer.max_scroll_y > 0
    return viewer.scroll_y / viewer.max_scroll_y


async def test_h_collapses_the_tldr_to_one_line_and_back_keeping_the_reading_position(make_app):
    llm = ScriptedLlm()  # no replies: the TL;DR is cached, so any call would fail the test
    app = make_app(resolver=fake_resolver(llm))
    for item_id in seed(app):
        cache_tldr(app, item_id)
        StateRepo(app.conn).set_progress(item_id, 0.5, now=NOW)
    async with app.run_test(size=(180, 30)) as pilot:
        await open_and_settle(pilot)
        box = app.query_one("#reader-tldr-box")
        viewer = app.query_one(ReaderPane).viewer
        assert "Cached point." in tldr(app) and box.outer_size.height > 3
        assert position(app) == pytest.approx(0.5, abs=0.05)
        expanded_max = viewer.max_scroll_y
        await pilot.press("h")
        await until(pilot, lambda: tldr(app) == COLLAPSED and app.reader_settled)
        assert box.display and box.border_title == "TL;DR"  # the same box, on one line
        assert box.outer_size.height == 3  # the line and its border
        assert viewer.max_scroll_y < expanded_max  # the article got the room back...
        assert position(app) == pytest.approx(0.5, abs=0.05)  # ...and didn't jump
        await pilot.press("h")
        await until(pilot, lambda: "Cached point." in tldr(app) and app.reader_settled)
        assert viewer.max_scroll_y == expanded_max
        assert position(app) == pytest.approx(0.5, abs=0.05)
        assert llm.requests == [] and summarize_runs(app) == []


async def test_collapsed_writes_no_tldr_until_h(make_app):
    llm = ScriptedLlm(replies=[summary_reply("Written on h.")])
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == NOT_WRITTEN and collapsed(app)
        await pilot.press("n")  # n/p don't ask either
        await settle(pilot)
        await pilot.press("p")
        await settle(pilot)
        assert tldr(app) == NOT_WRITTEN
        assert llm.requests == [] and summarize_runs(app) == []
        await pilot.press("h")  # expanding asks, exactly as Enter does when shown
        await until(pilot, lambda: "Written on h." in tldr(app) and not busy(app))
        assert not collapsed(app) and app.query_one("#reader-tldr", Static).display
        assert len(llm.requests) == 1 and [s for s, _ in summarize_runs(app)] == ["ok"]


async def test_a_cached_tldr_shows_collapsed_and_h_opens_it_without_a_call(make_app):
    llm = ScriptedLlm()
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(llm))
    for item_id in seed(app):
        cache_tldr(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await until(pilot, lambda: tldr(app) == COLLAPSED)  # the preview, too
        await open_and_settle(pilot)
        assert tldr(app) == COLLAPSED
        await pilot.press("h")
        await until(pilot, lambda: "Cached point." in tldr(app) and not busy(app))
        assert llm.requests == [] and summarize_runs(app) == []


async def test_the_last_toggle_is_remembered_over_the_config_default(make_app, paths):
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(ScriptedLlm()))
    for item_id in seed(app):
        cache_tldr(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == COLLAPSED  # no saved toggle yet: config.toml's default
        await pilot.press("h")
        await until(pilot, lambda: "Cached point." in tldr(app))
        assert load(paths).tldr_collapsed is False  # saved at once
    again = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(ScriptedLlm()))
    async with again.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert "Cached point." in tldr(again) and not collapsed(again)  # the saved toggle wins
    forgetful = config(tldr="collapsed", remember_state=False)
    third = make_app(config=forgetful, resolver=fake_resolver(ScriptedLlm()))
    async with third.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(third) == COLLAPSED  # without remember_state, config.toml's every launch


async def test_a_saved_state_without_the_toggle_uses_the_config_default(make_app, paths):
    save(paths, UiState(theme="nord"))  # written before P12, or before h was ever pressed
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(ScriptedLlm()))
    for item_id in seed(app):
        cache_tldr(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == COLLAPSED and app.theme == "nord"


async def test_reopening_the_last_article_collapsed_never_calls_the_model(make_app, paths):
    llm = ScriptedLlm()
    reopen = config(reopen_last_article=True, tldr="collapsed")
    app = make_app(config=reopen, resolver=fake_resolver(llm))
    ids = seed(app)
    save(paths, UiState(reading_item_id=ids[0], reading_progress=0.3))
    async with app.run_test(size=(180, 50)) as pilot:
        await settle(pilot)
        assert app.reading_id == ids[0] and tldr(app) == NOT_WRITTEN
        assert llm.requests == [] and summarize_runs(app) == []


async def test_h_in_the_list_does_nothing(make_app, paths):
    app = make_app(resolver=fake_resolver(ScriptedLlm()))
    for item_id in seed(app):
        cache_tldr(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await until(pilot, lambda: "Cached point." in tldr(app))
        await pilot.press("h")
        await pilot.pause()
        assert app.mode == "NORMAL" and "Cached point." in tldr(app) and not collapsed(app)
        await pilot.press("enter")  # the reader opens as before
        await settle(pilot)
        await pilot.press("escape", "h")  # the article stays beside the list; h still does nothing
        await pilot.pause()
        assert app.mode == "NORMAL" and "Cached point." in tldr(app) and not collapsed(app)
    assert load(paths).tldr_collapsed is None


@pytest.mark.parametrize("start", ["shown", "collapsed"])
async def test_without_a_key_the_box_stays_hidden_and_h_does_nothing(make_app, paths, start):
    app = make_app(config=config(tldr=start))  # the scrubbed environment has no key
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        await pilot.press("h")
        await pilot.pause()
        assert tldr(app) == "" and not app.query_one("#reader-tldr-box").display
        assert not app._notifications and summarize_runs(app) == []  # silently
        assert len(app.query("MarkdownH2")) == 4  # the reader works as in M1
    assert load(paths).tldr_collapsed is None


async def test_t_while_collapsed_expands_and_writes_one(make_app):
    llm = ScriptedLlm(replies=[summary_reply("Written on T.")])
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == NOT_WRITTEN
        await pilot.press("T")
        await until(pilot, lambda: "Written on T." in tldr(app) and not busy(app))
        assert not collapsed(app) and len(llm.requests) == 1


async def test_toggling_while_summarizing_never_asks_twice(make_app):
    llm = GatedLlm(replies=[summary_reply("Only once.")])
    app = make_app(resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await until(pilot, lambda: tldr(app) == "Summarizing…")
        await pilot.press("h")
        assert tldr(app) == SUMMARIZING
        await pilot.press("h")  # expanded again while it's on its way: nothing new is asked
        assert tldr(app) == "Summarizing…"
        await pilot.press("h")
        llm.gate.set()  # it arrives while collapsed, and stays collapsed
        await settle(pilot)
        assert tldr(app) == COLLAPSED
        await pilot.press("h")
        await until(pilot, lambda: "Only once." in tldr(app) and not busy(app))
        assert len(llm.requests) == 1 and [s for s, _ in summarize_runs(app)] == ["ok"]


async def test_collapsing_again_before_the_request_starts_asks_nothing(make_app):
    llm = ScriptedLlm(replies=[summary_reply()])
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(llm))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        app.action_toggle_tldr()  # expanding starts load_summary...
        app.action_toggle_tldr()  # ...which only runs once this is collapsed again
        await until(pilot, lambda: not busy(app))
        assert tldr(app) == NOT_WRITTEN and collapsed(app)
        assert llm.requests == [] and summarize_runs(app) == []


async def test_expanding_keeps_the_budget_guard(make_app):
    llm = ScriptedLlm(replies=[summary_reply()])
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(llm))
    seed(app)
    runs = RunsRepo(app.conn)
    runs.add_usage(
        runs.start("scout", now=NOW), tokens_in=1, tokens_out=1, cost_usd=5.0, unpriced_tokens=0
    )
    async with app.run_test(size=(180, 50)) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == NOT_WRITTEN
        await pilot.press("h")
        await until(pilot, lambda: "No TL;DR today" in tldr(app) and not busy(app))
        assert llm.requests == [] and summarize_runs(app) == []


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])  # full screen; the narrowest side by side
async def test_the_longest_collapsed_line_fits_on_one_line(make_app, size):
    app = make_app(config=config(tldr="collapsed"), resolver=fake_resolver(ScriptedLlm()))
    seed(app)
    async with app.run_test(size=size) as pilot:
        await open_and_settle(pilot)
        assert tldr(app) == NOT_WRITTEN
        body = app.query_one("#reader-tldr", Static)
        assert cell_len(NOT_WRITTEN) <= body.content_size.width  # whole, not cut to "…"
        assert app.query_one("#reader-tldr-box").outer_size.height == 3


async def test_h_is_in_the_help_but_not_in_the_hints_row(make_app):
    assert "h" not in {hint.key for hint in KEYMAP["READ"]}  # it would push z:zen off at 90
    app = make_app()
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpOverlay))
        body = str(app.screen.query_one("#help-text").render())
        assert "h          hide/show TL;DR" in body
