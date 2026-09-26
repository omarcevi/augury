"""P1 (the theme persists) and P2 (the last state comes back), through ui_state.json."""

from datetime import timedelta

import pytest
from textual import constants
from textual.widgets import ContentSwitcher, Input

from augury.agents.normalize import store_items
from augury.core.config import Config, ScoutConfig, TuiConfig
from augury.core.db.state_repo import StateRepo
from augury.core.models import RawItem
from augury.tui.app import AuguryApp
from augury.tui.query import DIGEST_PRESET, ItemFilter
from augury.tui.ui_state import FilterState, UiState, load, save
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.reader_pane import ReaderPane
from tests.tui.conftest import NOW, until
from tests.tui.test_reader import LONG_MD, cache


def config(**tui) -> Config:
    return Config(scout=ScoutConfig(auto_after_hours=0), tui=TuiConfig(**tui))


def seed(app) -> list[str]:
    """Three blog posts (Post 0 is the newest) and a paper."""
    posts = [
        RawItem(source_id="hf-blog", url=f"https://x/{i}", title=f"Post {i}", summary=f"Post {i}.")
        for i in range(3)
    ]
    ids = [
        store_items(app.conn, [raw], now=NOW - timedelta(minutes=i)).new_ids[0]
        for i, raw in enumerate(posts)
    ]
    paper = RawItem(
        source_id="hf-papers",
        kind="paper",
        arxiv_id="2609.00001",
        url="https://huggingface.co/papers/2609.00001",
        title="A paper",
    )
    store_items(app.conn, [paper], now=NOW - timedelta(minutes=10))
    return ids


def current_id(app) -> str | None:
    row = app.query_one(ItemsTable).current_row()
    return row.id if row else None


def view(app) -> str | None:
    return app.query_one(ContentSwitcher).current


# --- P1 · the theme persists ---------------------------------------------------------------


async def test_a_theme_picked_with_t_survives_a_restart(make_app, paths):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("t")
        picked = app.theme
        assert picked != "textual-dark" and load(paths).theme == picked  # saved at once
    again = make_app()
    async with again.run_test(size=(140, 40)):
        assert again.theme == picked
        assert again.query_one("#chip-theme", Chip).value == picked


async def test_config_toml_decides_until_a_theme_is_picked(make_app, paths):
    app = make_app(config=config(theme="nord"))
    async with app.run_test(size=(140, 40)):
        assert app.theme == "nord" and app.query_one("#chip-theme", Chip).value == "nord"
    # Launching never writes the configured theme, so editing config.toml still works.
    assert load(paths).theme is None
    again = make_app(config=config(theme="dracula"))
    async with again.run_test(size=(140, 40)):
        assert again.theme == "dracula"


async def test_a_theme_set_any_other_way_also_persists(make_app, paths):
    app = make_app()
    async with app.run_test(size=(140, 40)):
        app.theme = "gruvbox"  # e.g. from the ctrl+p theme picker
    assert load(paths).theme == "gruvbox"


@pytest.mark.parametrize(
    ("saved", "configured", "expected"),
    [("no-such-theme", "dracula", "dracula"), ("no-such-theme", "nor-this", "textual-dark")],
)
async def test_an_unavailable_theme_falls_back(make_app, paths, saved, configured, expected):
    save(paths, UiState(theme=saved))
    app = make_app(config=config(theme=configured))
    async with app.run_test(size=(140, 40)):
        assert app.theme == expected and app.query_one("#chip-theme", Chip).value == expected


async def test_the_theme_persists_even_without_remember_state(make_app, paths):
    save(paths, UiState(theme="nord", filter=FilterState(show="all")))
    app = make_app(config=config(remember_state=False))
    async with app.run_test(size=(140, 40)):
        assert app.theme == "nord" and app.item_filter == DIGEST_PRESET


# --- P2 · remember the last state ------------------------------------------------------------


async def test_filters_search_view_and_row_come_back_after_quitting(make_app, paths):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("S", "space", "enter")  # hf-blog
        await pilot.press("D", "down", "enter")  # 7 days
        await pilot.press("s", "v", "v")  # Popular, All
        await pilot.press("slash", "p", "o", "s", "t", "enter")
        await pilot.press("down")
        await pilot.pause()
        chosen, chosen_filter = current_id(app), app.item_filter
        assert chosen_filter == ItemFilter(
            search="post", sources=frozenset({"hf-blog"}), date="7d", sort="popular", show="all"
        )
        assert chosen is not None and app.query_one(ItemsTable).cursor_row == 1
        await pilot.press("2", "q")
    again = make_app()
    seed_count = again.conn.execute("SELECT count(*) FROM items").fetchone()[0]
    assert seed_count == 4
    async with again.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert again.item_filter == chosen_filter
        assert again.query_one("#search", Input).value == "post"
        assert again.query_one("#chip-sources", Chip).value == "hf-blog"
        assert again.query_one("#chip-show", Chip).value == "All"
        assert again.mode == "SOURCES" and view(again) == "sources-view"
        await pilot.press("1")
        await pilot.pause()
        assert current_id(again) == chosen and again.mode == "NORMAL"


async def test_a_restored_search_does_not_reset_the_restored_row(make_app, paths):
    app = make_app()
    ids = seed(app)
    save(paths, UiState(filter=FilterState(search="post"), selected_item_id=ids[2]))
    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(5):  # a re-applied search would re-query and move the cursor to the top
            await pilot.pause()
        assert current_id(app) == ids[2]


async def test_without_remember_state_launch_uses_the_digest_defaults(make_app, paths):
    app = make_app(config=config(remember_state=False, reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[1])
    save(
        paths,
        UiState(
            current_visit_started_at=NOW - timedelta(days=1),
            filter=FilterState(show="all", search="post"),
            view="sources",
            selected_item_id=ids[2],
            reading_item_id=ids[1],
            reading_progress=0.5,
        ),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.item_filter == DIGEST_PRESET
        assert app.query_one("#search", Input).value == ""
        assert view(app) == "main" and app.mode == "NORMAL"
        assert current_id(app) == ids[0] and app.reading_id is None
        assert app.last_visit == NOW - timedelta(days=1)  # the visit is still tracked (P11)
    assert load(paths).current_visit_started_at == NOW


async def test_sources_and_items_that_no_longer_exist_are_skipped(make_app, paths):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    save(
        paths,
        UiState(
            filter=FilterState(sources=["hf-blog", "removed-blog"], kinds=["podcast"]),
            selected_item_id="web:gone",
            reading_item_id="web:also-gone",
            reading_progress=0.5,
        ),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.item_filter == ItemFilter(sources=frozenset({"hf-blog"}))
        assert app.query_one("#chip-sources", Chip).value == "hf-blog"
        assert current_id(app) == ids[0] and app.reading_id is None and app.mode == "NORMAL"


@pytest.mark.parametrize("raw", [b"", b"{broken", b'{"filter": {"show": 42}, "view": "moon"}'])
async def test_a_missing_or_corrupt_file_starts_with_the_defaults(make_app, paths, raw):
    paths.ui_state_file.write_bytes(raw)
    app = make_app()
    ids = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.item_filter == DIGEST_PRESET and view(app) == "main"
        assert current_id(app) == ids[0]
    assert load(paths).current_visit_started_at == NOW  # and the file is valid again


async def test_changes_are_saved_while_running_not_only_on_quit(make_app, paths, monkeypatch):
    monkeypatch.setattr(AuguryApp, "UI_STATE_SAVE_S", 0.01)
    app = make_app()
    ids = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("v", "down")
        await until(pilot, lambda: load(paths).filter.show == "new")
        await until(pilot, lambda: load(paths).selected_item_id == ids[1])
        await pilot.press("2")
        await until(pilot, lambda: load(paths).view == "sources")


async def read_halfway(app, pilot, item_id: str) -> float:
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await until(pilot, lambda: app.reader_settled)
    viewer = app.query_one(ReaderPane).viewer
    viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
    await until(pilot, lambda: StateRepo(app.conn).get(item_id).read_progress > 0)
    return viewer.scroll_y / viewer.max_scroll_y


async def test_the_last_article_reopens_where_it_was_left(make_app, paths, monkeypatch):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[0], LONG_MD)
    async with app.run_test(size=(180, 50)) as pilot:
        left_at = await read_halfway(app, pilot, ids[0])
        await pilot.press("q")
    assert load(paths).reading_item_id == ids[0]
    assert load(paths).reading_progress == pytest.approx(left_at)

    recorded: list[float] = []
    real = StateRepo.set_progress
    monkeypatch.setattr(
        StateRepo,
        "set_progress",
        lambda self, item_id, p, *, now: (recorded.append(p), real(self, item_id, p, now=now)),
    )
    again = make_app(config=config(reopen_last_article=True))
    async with again.run_test(size=(180, 50)) as pilot:
        assert again.reading_id == ids[0] and again.mode == "READ"
        await again.workers.wait_for_complete()
        await until(pilot, lambda: again.reader_settled)
        viewer = again.query_one(ReaderPane).viewer
        assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(left_at, abs=0.02)
        # Opened (so read), yet listed under Unread and selected: the open item is pinned.
        assert current_id(again) == ids[0] and again.item_filter.show == "unread"
        await pilot.press("j")
        await pilot.pause()
    assert all(p == pytest.approx(left_at, abs=0.05) for p in recorded)  # never a bogus 0


async def test_the_exact_position_wins_over_the_furthest_read(make_app, paths):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[1], LONG_MD)
    StateRepo(app.conn).set_progress(ids[1], 0.8, now=NOW)  # read to 80 %, then scrolled back up
    save(paths, UiState(reading_item_id=ids[1], reading_progress=0.3))
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.reader_settled)
        viewer = app.query_one(ReaderPane).viewer
        assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.3, abs=0.02)
        assert StateRepo(app.conn).get(ids[1]).read_progress == 0.8


async def test_the_last_article_stays_closed_by_default(make_app, paths):
    app = make_app()
    ids = seed(app)
    cache(app, ids[1], LONG_MD)
    save(paths, UiState(selected_item_id=ids[1], reading_item_id=ids[1], reading_progress=0.5))
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause()
        assert app.reading_id is None and app.mode == "NORMAL"
        assert current_id(app) == ids[1]  # the row is still selected


async def test_an_article_left_open_behind_the_sources_view_comes_back_there(make_app, paths):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[0], LONG_MD)
    save(paths, UiState(view="sources", reading_item_id=ids[0], reading_progress=0.4))
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        assert app.mode == "SOURCES" and app.reading_id == ids[0]
        await pilot.press("1")
        await until(pilot, lambda: app.reader_settled)
        assert app.mode == "READ"


async def test_quitting_from_behind_a_modal_still_saves(make_app, paths):
    app = make_app()
    ids = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("v", "down", "question_mark")
        await pilot.press("ctrl+q")  # quits with the help overlay still open
    assert load(paths).filter.show == "new" and load(paths).selected_item_id == ids[1]


async def test_quitting_records_when_the_visit_ended(make_app, paths):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        assert load(paths).current_visit_ended_at is None
        await pilot.press("q")
    assert load(paths).current_visit_ended_at == NOW


async def test_a_quick_relaunch_keeps_items_new(make_app, paths):
    save(paths, UiState(current_visit_started_at=NOW - timedelta(days=1)))
    app = make_app()
    ids = seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("q")  # a look of a few seconds (the test clock doesn't move at all)
    again = make_app()
    async with again.run_test(size=(140, 40)):
        assert again.last_visit == NOW - timedelta(days=1)
        table = again.query_one(ItemsTable)
        assert table.get_row(ids[0])[1].plain.startswith("✦ ")


async def test_a_failing_save_never_stops_quitting(make_app, monkeypatch, tmp_path):
    log_file = tmp_path / "textual.log"
    monkeypatch.setattr(constants, "LOG_FILE", str(log_file))

    def broken() -> None:
        raise RuntimeError("ui state went wrong")

    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        monkeypatch.setattr(app, "save_ui_state", broken)
        await pilot.press("q")
    assert app.return_code == 0  # a clean quit, not a crash
    assert "ui state went wrong" in log_file.read_text(encoding="utf-8")


def updated_at(app, item_id: str) -> str:
    return app.conn.execute(
        "SELECT updated_at FROM item_state WHERE item_id = ?", (item_id,)
    ).fetchone()[0]


async def test_reopening_at_launch_is_not_counted_as_an_open(make_app, paths):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[0], LONG_MD)
    state = StateRepo(app.conn)
    state.mark_opened(ids[0], now=NOW - timedelta(hours=1))  # the last run's open
    state.set_progress(ids[0], 0.3, now=NOW - timedelta(hours=1))  # so landing writes nothing
    read_at, touched = state.get(ids[0]).read_at, updated_at(app, ids[0])
    save(paths, UiState(reading_item_id=ids[0], reading_progress=0.3))
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.reader_settled)
        assert app.reading_id == ids[0]
        assert state.interactions(ids[0]) == ["open"]  # only the last run's
        assert state.get(ids[0]).read_at == read_at and updated_at(app, ids[0]) == touched
        await pilot.press("escape", "enter")  # opening it yourself still counts
        await app.workers.wait_for_complete()
        assert state.interactions(ids[0]) == ["open", "open"]


async def test_the_first_progress_written_after_a_reopen_is_where_it_landed(
    make_app, paths, monkeypatch
):
    app = make_app(config=config(reopen_last_article=True))
    ids = seed(app)
    cache(app, ids[0], LONG_MD)
    save(paths, UiState(reading_item_id=ids[0], reading_progress=0.5))  # the DB still says 0
    recorded: list[float] = []
    real = StateRepo.set_progress
    monkeypatch.setattr(
        StateRepo,
        "set_progress",
        lambda self, item_id, p, *, now: (recorded.append(p), real(self, item_id, p, now=now)),
    )
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.reader_settled and bool(recorded))
    assert recorded  # so the check below can't pass vacuously
    assert all(p == pytest.approx(0.5, abs=0.02) for p in recorded)  # never 0 on the way
