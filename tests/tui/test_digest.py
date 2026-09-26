"""F27: the ranked digest in the TUI -- the Score column and sort, why-read and the ranking's
explanation in the preview, the hidden-by-triage line, the Tags picker and the degraded banner."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from textual.widgets import SelectionList, Static

from augury.agents.normalize import store_items
from augury.agents.rank import build_digest
from augury.core.clock import local_day
from augury.core.config import RankingConfig
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.state_repo import StateRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import RawItem, TriageResult
from augury.tui.digest_view import breakdown_line, hidden_line, score_text
from augury.tui.query import TriageHidden
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import PickerModal
from augury.tui.widgets.status_line import StatusLine
from tests.tui.conftest import NOW

Spec = tuple[str, int, list[str], list[str]]  # title, relevance, flags, tags


def seed_digest(app, specs: list[Spec]) -> dict[str, str]:
    raws = [
        RawItem(
            source_id="hf-blog",
            url=f"https://x/{i}",
            title=spec[0],
            summary=f"Summary of {spec[0]}.",
        )
        for i, spec in enumerate(specs)
    ]
    ids = store_items(app.conn, raws, now=NOW).new_ids
    results = [
        TriageResult(
            item_id=item_id,
            relevance=relevance,
            why_read=f"Why read {title}.",
            tags=tags,
            flags=flags,
        )
        for item_id, (title, relevance, flags, tags) in zip(ids, specs, strict=True)
    ]
    TriageRepo(app.conn).save_all(results, run_id="r1", model="fake/fake-model", prompt_version=1)
    build_digest(app.conn, local_day(NOW), now=NOW, config=RankingConfig(), ai=True)
    return {spec[0]: item_id for spec, item_id in zip(specs, ids, strict=True)}


def titles(app) -> list[str]:
    return [row.title for row in app.query_one(ItemsTable).rows_by_key.values()]


def header(app) -> str:
    return str(app.query_one("#reader-header", Static).content)


async def test_the_default_view_is_the_digest_sorted_by_score(make_app):
    app = make_app()
    ids = seed_digest(app, [("Low", 2, [], []), ("High", 9, [], []), ("Mid", 5, [], [])])
    async with app.run_test(size=(180, 50)):
        assert titles(app) == ["High", "Mid", "Low"]
        assert app.query_one("#chip-sort", Chip).value == "Score ↓"
        cell = app.query_one(ItemsTable).get_cell(ids["High"], "score")
        assert cell.plain == "9.2" and str(cell.style) == app.get_css_variables()["success"]


async def test_promo_and_thin_items_wait_behind_the_hidden_row(make_app):
    app = make_app()
    ids = seed_digest(
        app, [("Clean", 5, [], []), ("Ad", 6, ["promo"], []), ("Stub", 4, ["thin"], [])]
    )
    async with app.run_test(size=(180, 50)) as pilot:
        row = app.query_one("#triage-hidden", Static)
        assert titles(app) == ["Clean"]
        assert row.display and "hidden by triage (2)" in str(row.content)
        assert "promo ×1" in str(row.content) and "thin ×1" in str(row.content)  # noqa: RUF001
        await pilot.press("H")
        assert titles(app) == ["Ad", "Clean", "Stub"]
        assert app.query_one(ItemsTable).get_cell(ids["Ad"], "title").plain.endswith("⚑")
        assert "showing 2 hidden by triage" in str(row.content)
        await pilot.press("H")
        assert titles(app) == ["Clean"] and "hidden by triage (2)" in str(row.content)


async def test_off_topic_items_stay_visible_near_the_bottom(make_app):
    app = make_app()
    seed_digest(app, [("Aside", 1, ["off_topic"], []), ("Core", 8, [], [])])
    async with app.run_test(size=(180, 50)):
        assert titles(app) == ["Core", "Aside"]
        assert not app.query_one("#triage-hidden", Static).display


async def test_the_tags_picker_filters_the_table(make_app):
    app = make_app()
    seed_digest(
        app, [("A", 5, [], ["agents", "rag"]), ("B", 6, [], ["agents"]), ("C", 7, [], ["vision"])]
    )
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("#")  # pre-flight: G is "go to bottom" since M1.1
        assert isinstance(app.screen, PickerModal)
        await pilot.press("space", "enter")  # tick the first option: agents, the most common
        await pilot.pause()
        assert app.item_filter.tags == frozenset({"agents"})
        assert sorted(titles(app)) == ["A", "B"]
        assert app.query_one("#chip-tags", Chip).value == "agents"


async def test_a_picked_tag_the_view_no_longer_has_can_still_be_unticked(make_app):
    app = make_app()
    seed_digest(app, [("A", 5, [], ["agents"])])
    async with app.run_test(size=(180, 50)) as pilot:
        app.apply_filter(replace(app.item_filter, tags=frozenset({"gone"})))
        await pilot.press("#")
        await pilot.pause()
        picker = app.screen.query_one("#picker", SelectionList)
        assert [str(option.prompt) for option in picker.options] == ["agents (1)", "gone (0)"]
        assert picker.selected == ["gone"]
        await pilot.press("down", "space", "enter")  # untick it
        await pilot.pause()
        assert app.item_filter.tags == frozenset() and titles(app) == ["A"]


async def test_without_tags_the_picker_says_where_they_come_from(make_app):
    app = make_app()
    store_items(app.conn, [RawItem(source_id="hf-blog", url="https://x/1", title="T")], now=NOW)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("#")
        await pilot.pause()
        assert not isinstance(app.screen, PickerModal)
        notes = [(n.message, n.markup) for n in app._notifications]
        assert notes == [("No tags yet: they come from AI triage.", False)]


async def test_why_read_and_the_breakdown_show_in_the_preview(make_app):
    app = make_app()
    seed_digest(app, [("High", 9, [], []), ("Low", 2, [], [])])
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await pilot.pause()
        assert "Why read High." in header(app)
        assert "score 9.2 = relevance 0.90" in header(app) and "recency 1.00" in header(app)
        assert "High  hf-blog  9.2" in app.query_one(StatusLine).selection.plain


async def test_a_scout_without_ai_shows_the_degraded_banner(make_app):
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW)
    triage = {"attempted": 3, "degraded": "AI not configured", "degraded_kind": "not_configured"}
    runs.finish(run_id, "ok", now=NOW, stats={"triage": triage})
    async with app.run_test(size=(140, 40)):
        banner = app.query_one("#banner", Static)
        assert banner.display and "ranking degraded: AI not configured" in str(banner.content)


async def test_no_banner_when_triage_worked(make_app):
    app = make_app()
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW)
    runs.finish(run_id, "ok", now=NOW, stats={"triage": {"attempted": 3, "triaged": 3}})
    async with app.run_test(size=(140, 40)):
        assert not app.query_one("#banner", Static).display


async def test_hostile_why_read_tags_and_banner_render_literally(make_app):
    app = make_app()
    ids = seed_digest(app, [("T", 5, [], []), ("U", 1, [], [])])
    hostile = TriageResult(
        item_id=ids["T"],
        relevance=5,
        why_read="[bold red]boom[/]",
        tags=["[link=https://e.vil]x[/link]"],
    )
    TriageRepo(app.conn).save_all([hostile], run_id="r2", model="m", prompt_version=1)
    runs = RunsRepo(app.conn)
    run_id = runs.start("scout", now=NOW)
    triage = {"degraded": "provider said [b]no[/b]", "degraded_kind": "provider"}
    runs.finish(run_id, "ok", now=NOW, stats={"triage": triage})
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await pilot.pause()
        assert "[bold red]boom[/]" in header(app)
        assert "[link=" in app.query_one(ItemsTable).get_cell(ids["T"], "tags").plain
        assert "provider said [b]no[/b]" in str(app.query_one("#banner", Static).content)
        await pilot.press("#")
        await pilot.pause()
        assert isinstance(app.screen, PickerModal)
        picker = app.screen.query_one("#picker", SelectionList)
        labels = [str(option.prompt) for option in picker.options]
        assert "[link=https://e.vil]x[/link] (1)" in labels


async def test_without_a_key_the_preview_explains_the_likes_ranking(make_app):
    # Pre-flight, user decision 2026-09-26: no key ranks by your likes and recency.
    app = make_app()
    old = store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url="https://x/old", title="Latent diffusion tricks")],
        now=NOW - timedelta(days=2),
    ).new_ids
    StateRepo(app.conn).toggle(old[0], "liked", now=NOW)
    store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url="https://x/new", title="A diffusion primer")],
        now=NOW,
    )
    build_digest(app.conn, local_day(NOW), now=NOW, config=RankingConfig(), ai=False)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await pilot.pause()
        assert "matches your likes: diffusion" in header(app) and "source you like" in header(app)
        await pilot.press("3")
        assert app.check_action("pick_tags", ()) is False  # items-only actions are off here
        assert app.check_action("toggle_triage_hidden", ()) is False


async def test_a_source_never_liked_is_not_called_one_you_like(make_app):
    # Task 26 review: the smoothed like rate gives a source nobody has read or liked a high
    # score next to one with a single like among several reads; that isn't "a source you like".
    app = make_app()
    liked = store_items(
        app.conn,
        [
            RawItem(source_id="hf-papers", url=f"https://x/p{i}", title=f"Diffusion paper {i}")
            for i in range(5)
        ],
        now=NOW - timedelta(days=2),
    ).new_ids
    state = StateRepo(app.conn)
    for item_id in liked:
        state.mark_opened(item_id, now=NOW)
    state.toggle(liked[0], "liked", now=NOW)
    store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url="https://x/new", title="A diffusion primer")],
        now=NOW,
    )
    build_digest(app.conn, local_day(NOW), now=NOW, config=RankingConfig(), ai=False)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "up")
        await pilot.pause()
        row = app.query_one(ItemsTable).current_row()
        assert row is not None and row.source_id == "hf-blog"
        data = json.loads(row.breakdown_json or "{}")
        assert data["mode"] == "likes" and data["terms"]["source"] >= 0.5  # high, yet never liked
        assert "matches your likes: diffusion" in header(app)
        assert "source you like" not in header(app)


def test_breakdown_line_explains_each_mode():
    ai = {
        "final": 0.9231,
        "mode": "ai",
        "terms": {"rel": 0.9, "rec": 1.0},
        "weights": {"rel": 0.7692, "rec": 0.2308},
    }
    assert breakdown_line(json.dumps(ai)) == (
        "score 9.2 = relevance 0.90×0.77 + recency 1.00×0.23"  # noqa: RUF001
    )
    likes = {
        "final": 0.7,
        "mode": "likes",
        "terms": {"topic": 1.0, "source": 0.9, "rec": 0.5},
        "weights": {"topic": 0.45, "source": 0.2, "rec": 0.35},
        "matched": ["diffusion", "rl"],
        "age_hours": 50,
    }
    liked = breakdown_line(json.dumps(likes), source_liked=True)
    assert liked == "score 7.0 · matches your likes: diffusion, rl · source you like · 2d ago"
    assert "source you like" not in breakdown_line(json.dumps(likes))  # never liked from it
    low = {**likes, "terms": {**likes["terms"], "source": 0.4}, "matched": [], "age_hours": 0}
    assert breakdown_line(json.dumps(low), source_liked=True) == "score 7.0 · just now"
    cold = {**ai, "mode": "cold", "terms": {"rec": 0.8}, "weights": {"rec": 1.0}}
    assert breakdown_line(json.dumps(cold)) == "score 9.2 = recency 0.80×1.00"  # noqa: RUF001


@pytest.mark.parametrize("broken", [None, "", "{}", "[]", "not json", '{"final": "x"}'])
def test_a_broken_breakdown_explains_nothing(broken):
    assert breakdown_line(broken) == ""


def test_score_text_colours_by_band():
    palette = {"success": "green", "warning": "yellow"}
    shown = [score_text(score, palette) for score in (0.8, 0.6, 0.59)]
    assert [(t.plain, str(t.style)) for t in shown] == [
        ("8.0", "green"),
        ("6.0", "yellow"),
        ("5.9", "dim"),
    ]
    assert score_text(None, palette).plain == "—"


def test_the_hidden_line_counts_each_flag():
    hidden = TriageHidden(3, {"thin": 1, "promo": 2})
    assert hidden_line(hidden, expanded=False) == (
        "── hidden by triage (3): promo ×2 · thin ×1 · H shows them ──"  # noqa: RUF001
    )
    assert hidden_line(hidden, expanded=True) == (
        "── showing 3 hidden by triage · H hides them again ──"
    )


async def test_o_keeps_a_promo_row_listed_outside_the_top_views(make_app):
    # Show: Saved lists promo items; `o` re-reads the rows (for their read state) and must
    # not drop one because the top views leave promo out.
    app = make_app()
    ids = seed_digest(app, [("Ad", 6, ["promo"], []), ("Clean", 5, [], [])])
    StateRepo(app.conn).toggle(ids["Ad"], "saved", now=NOW)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("v", "v", "v")  # Unread → New → All → Saved
        assert app.item_filter.show == "saved" and titles(app) == ["Ad"]
        app.open_url = lambda url, *, new_tab=True: None  # type: ignore[method-assign]
        await pilot.press("o")
        await pilot.pause()
        assert titles(app) == ["Ad"]


@pytest.mark.parametrize("size", [(90, 40), (130, 40), (180, 50)])
def test_digest_snapshots(make_app, snap_compare, size):
    app = make_app()
    seed_digest(
        app,
        [
            ("Speculative decoding at scale", 9, [], ["inference", "decoding"]),
            ("A friendly agents primer", 6, [], ["agents"]),
            ("Our product launch", 5, ["promo"], []),
            ("Notes on vision transformers", 3, ["off_topic"], ["vision"]),
        ],
    )
    assert snap_compare(app, terminal_size=size)
