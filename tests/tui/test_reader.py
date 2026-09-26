import asyncio
from datetime import timedelta

import pytest
from textual import constants
from textual.app import App
from textual.widgets import Static

from augury.agents.normalize import store_items
from augury.core.config import Config
from augury.core.db.contents_repo import ContentsRepo
from augury.core.db.open import open_db
from augury.core.db.state_repo import StateRepo
from augury.core.models import Content, RawItem
from augury.extract.service import EXTRACTOR_VERSION
from augury.tui.app import AuguryApp
from augury.tui.layout import layout_for
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.reader_pane import ReaderPane
from augury.tui.widgets.status_line import StatusLine
from tests.helpers import CountingHttp
from tests.tui.conftest import NOW, Gate, until
from tests.unit.test_extract_html import GENERIC

LONG_MD = "# Title\n\n" + "\n\n".join(f"## Section {i}\n\n" + "word " * 150 for i in range(12))


def seed(app) -> list[str]:
    # Post 0 is the newest, so it's the row the cursor starts on.
    raws = [
        RawItem(
            source_id="hf-blog",
            url=f"https://x/{i}",
            title=f"Post {i}",
            summary=f"Summary of post {i}.",
            published_at=NOW - timedelta(hours=i + 1),
        )
        for i in range(3)
    ]
    return store_items(app.conn, raws, now=NOW).new_ids


def cache(app, item_id: str, md: str = LONG_MD) -> None:
    ContentsRepo(app.conn).save(
        Content(
            item_id=item_id,
            status="ok",
            body_md=md,
            extractor="test",
            extractor_version=EXTRACTOR_VERSION,
            word_count=len(md.split()),
            fetched_at=NOW,
        )
    )


async def open_first(pilot) -> None:
    await pilot.press("enter")
    await pilot.app.workers.wait_for_complete()
    await until(pilot, lambda: pilot.app.reader_settled)


def progress(app, item_id: str) -> float:
    return StateRepo(app.conn).get(item_id).read_progress


def test_layout_breakpoints():
    assert [layout_for(w) for w in (90, 130, 180)] == ["narrow", "medium", "wide"]


def test_the_reader_debounce_defaults_to_300_ms(paths, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", waits.append)
    app = AuguryApp(conn=open_db(paths, now=NOW), config=Config(), paths=paths)
    app.reader_debounce()
    assert waits == [0.3]


async def test_cursor_movement_never_touches_the_network(make_app):
    http = CountingHttp()
    app = make_app(http=http)
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("down", "down", "up")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert http.calls == []
        assert "Summary of post" in app.query_one(ReaderPane).preview_text


async def test_enter_renders_cached_content_without_network(make_app):
    http = CountingHttp()
    app = make_app(http=http)
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        assert http.calls == [] and app.mode == "READ"
        assert len(app.query("MarkdownH2")) == 12


async def test_enter_extracts_an_uncached_article(make_app):
    http = CountingHttp({"https://x/0": GENERIC})
    app = make_app(http=http)
    ids = seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        assert http.calls == ["https://x/0"]
        assert ContentsRepo(app.conn).get(ids[0]).status == "ok"  # type: ignore[union-attr]


async def test_failed_extraction_offers_browser(make_app):
    app = make_app(http=CountingHttp())  # every page 404s
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        assert "Press o to open it in your browser" in app.query_one(ReaderPane).status_message


async def test_escape_returns_to_the_table(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("escape")
        assert app.mode == "NORMAL" and app.focused is app.query_one(ItemsTable)


async def test_progress_is_saved(make_app):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        app.query_one(ReaderPane).viewer.scroll_end(animate=False)
        await until(pilot, lambda: progress(app, item_id) >= 0.9)


async def test_contents_and_zen(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("c")
        assert app.query_one(ReaderPane).viewer.show_table_of_contents is True
        await pilot.press("z")
        assert app.screen.has_class("zen")


async def test_contents_never_squeeze_out_the_document(make_app):
    app = make_app()
    long_heading = "## " + "A very long section heading from a real paper " * 3
    cache(app, seed(app)[0], f"{LONG_MD}\n\n{long_heading}\n\nwords")
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("c")
        viewer = app.query_one(ReaderPane).viewer
        await until(pilot, lambda: viewer.table_of_contents.size.width > 0)
        await pilot.pause()
        assert viewer.document.size.width >= viewer.size.width // 2


async def test_narrow_terminal_opens_the_reader_full_screen(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(90, 40)) as pilot:
        assert app.query_one(ReaderPane).display is False
        await open_first(pilot)
        assert app.query_one(ReaderPane).display is True
        assert app.query_one("#items-pane").display is False


class WaitingHttp(CountingHttp):
    """Reports a crawl delay for every request, then holds it until `release` is set."""

    def __init__(self, pages: dict[str, bytes] | None = None) -> None:
        super().__init__(pages)
        self.release = asyncio.Event()

    async def get(self, url, **kwargs):
        if self.on_wait:
            self.on_wait("arxiv.org", 15.0)
        await self.release.wait()
        return await super().get(url, **kwargs)


async def test_crawl_delay_is_explained(make_app):
    http = WaitingHttp({"https://x/0": GENERIC})
    app = make_app(http=http)
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        reader = app.query_one(ReaderPane)
        await until(pilot, lambda: "Waiting" in reader.status_message)
        assert "arxiv.org" in reader.status_message and "crawl delay" in reader.status_message
        http.release.set()


async def test_late_crawl_delay_notice_is_ignored(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("escape", "down")
        await pilot.pause()
        app.on_http_wait("arxiv.org", 15.0)  # e.g. an extraction the user already walked away from
        assert "crawl delay" not in app.query_one(ReaderPane).status_message


@pytest.mark.parametrize(("width", "columns"), [(90, 5), (130, 6), (180, 8)])
async def test_table_columns_fit_beside_the_reader(make_app, width, columns):
    app = make_app()
    seed(app)
    async with app.run_test(size=(width, 40)) as pilot:
        await pilot.pause()
        table = app.query_one(ItemsTable)
        assert len(table.columns) == columns and table.max_scroll_x == 0


async def test_resize_while_reading_keeps_the_open_row(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.resize_terminal(130, 50)
        await pilot.pause()
        table = app.query_one(ItemsTable)
        assert table.current_row().id == ids[0]  # type: ignore[union-attr]
        assert table.max_scroll_x == 0


@pytest.mark.parametrize("change", ["zen", "resize", "contents"])
async def test_relayout_keeps_the_reading_position(make_app, change):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id)
    async with app.run_test(size=(130, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
        await until(pilot, lambda: progress(app, item_id) > 0)
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.02)
        before = viewer.virtual_size
        if change == "zen":
            await pilot.press("z")
        elif change == "resize":
            await pilot.resize_terminal(200, 50)
        else:
            await pilot.press("c")
        await until(pilot, lambda: viewer.virtual_size != before and app.reader_settled)
        await pilot.press("j")
        await pilot.pause()
        assert progress(app, item_id) == pytest.approx(0.5, abs=0.05)
        assert viewer.scroll_y / viewer.max_scroll_y == pytest.approx(0.5, abs=0.05)


async def test_scrolling_during_the_switch_is_not_recorded(make_app):
    gate = Gate()
    gate.open()
    app = make_app(debounce=gate)
    ids = seed(app)
    cache(app, ids[0])
    cache(app, ids[1])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        viewer.scroll_to(y=viewer.max_scroll_y / 2, animate=False)
        await until(pilot, lambda: progress(app, ids[0]) > 0)
        gate.close()
        await pilot.press("n", "j")  # the old document is still on screen during the debounce
        await pilot.pause()
        gate.open()
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.reader_settled)
        assert app.reading_id == ids[1] and progress(app, ids[1]) == 0


async def test_skipped_items_are_not_marked_opened(make_app):
    gate = Gate()
    app = make_app(debounce=gate)
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter", "n", "n", "n")  # all inside the debounce; the last hits the end
        await pilot.pause()
        gate.open()
        await app.workers.wait_for_complete()
        await pilot.pause()
        state = StateRepo(app.conn)
        assert app.reading_id == ids[2]
        for skipped in ids[:2]:
            assert state.get(skipped).read_at is None and state.interactions(skipped) == []
        assert state.interactions(ids[2]) == ["open"]


async def test_prev_on_the_first_item_does_nothing(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("p")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app.reading_id == ids[0]
        assert StateRepo(app.conn).interactions(ids[0]) == ["open"]


async def test_actions_follow_the_open_item_after_a_requery(make_app, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(App, "open_url", lambda self, url, **kwargs: opened.append(url))
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(90, 40)) as pilot:  # narrow: the table is hidden meanwhile
        await open_first(pilot)
        await pilot.press("t", "s", "l", "x", "o")
        await pilot.pause()
        states = [StateRepo(app.conn).get(i) for i in ids]
        assert states[0].liked and states[0].hidden
        assert not any(s.liked or s.hidden for s in states[1:])
        assert opened == ["https://x/0"]


async def test_next_item_follows_the_open_item_after_a_requery(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("t")  # a theme change re-queries the table
        await pilot.pause()
        assert app.query_one(ItemsTable).current_row().id == ids[0]  # type: ignore[union-attr]
        await pilot.press("n")
        assert app.reading_id == ids[1]


async def test_escape_hint_survives_at_90_columns(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(90, 40)) as pilot:
        await open_first(pilot)
        hints_row = app.query_one(StatusLine).render_line(1).text
        assert "esc:back" in hints_row and "?:help" in hints_row and "q:quit" in hints_row


async def test_reader_errors_are_logged_with_a_traceback(make_app, monkeypatch, tmp_path):
    log_file = tmp_path / "textual.log"
    monkeypatch.setattr(constants, "LOG_FILE", str(log_file))

    class BrokenHttp(CountingHttp):
        async def get(self, url, **kwargs):
            raise RuntimeError("boom from the network layer")

    app = make_app(http=BrokenHttp())
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        assert "RuntimeError" in app.query_one(ReaderPane).status_message
    logged = log_file.read_text(encoding="utf-8")
    assert "Traceback" in logged and "boom from the network layer" in logged


async def test_next_item_opens_at_the_top(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    cache(app, ids[1])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        app.query_one(ReaderPane).viewer.scroll_end(animate=False)
        await until(pilot, lambda: progress(app, ids[0]) >= 0.9)
        await pilot.press("n")
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.reader_settled)
        assert app.reading_id == ids[1] and app.query_one(ReaderPane).viewer.scroll_y == 0
        assert progress(app, ids[1]) == 0


async def test_bracket_jumps_to_the_next_section(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        await pilot.press("right_square_bracket", "right_square_bracket")
        await pilot.pause()
        assert viewer.scroll_y == app.query("MarkdownH2").first().virtual_region.y
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert viewer.scroll_y == app.query_one("MarkdownH1").virtual_region.y


async def test_j_k_and_space_scroll_the_reader(make_app):
    app = make_app()
    cache(app, seed(app)[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        await pilot.press("j", "j", "j", "k")
        await pilot.pause()
        assert viewer.scroll_y == 2
        await pilot.press("space")
        await pilot.pause()
        assert viewer.scroll_y > 10


async def test_reopening_restores_the_reading_position(make_app):
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id)
    StateRepo(app.conn).set_progress(item_id, 0.5, now=NOW)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        assert viewer.scroll_y == pytest.approx(0.5 * viewer.max_scroll_y, abs=1)


async def test_resize_behind_a_modal_still_updates_the_layout(make_app):
    app = make_app()
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("question_mark")
        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.has_class("layout-narrow") and not app.screen.has_class("layout-wide")


async def test_row_dot_follows_the_reading_state(make_app):
    gate = Gate()
    app = make_app(debounce=gate)
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        table = app.query_one(ItemsTable)
        await pilot.press("enter")
        await pilot.pause()
        assert table.get_row(ids[0])[0].plain == "●"  # not opened yet: still in the debounce
        gate.open()
        await app.workers.wait_for_complete()
        assert table.get_row(ids[0])[0].plain == "◐"
        await until(pilot, lambda: app.reader_settled)
        app.query_one(ReaderPane).viewer.scroll_end(animate=False)
        await until(pilot, lambda: progress(app, ids[0]) >= 0.9)
        await pilot.press("n")
        await pilot.pause()
        assert table.get_row(ids[0])[0].plain == "○"


async def test_header_shows_the_reading_time_once_extracted(make_app):
    app = make_app(http=CountingHttp({"https://x/0": GENERIC}))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        assert " min" in str(app.query_one("#reader-header", Static).render())


async def test_like_while_reading_toggles_the_open_item(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("l")
        assert StateRepo(app.conn).get(ids[0]).liked
        assert ids[0] in app.query_one(ItemsTable).rows_by_key  # it doesn't drop out of Unread
        await pilot.press("l")
        assert not any(StateRepo(app.conn).get(i).liked for i in ids)


async def test_hiding_while_reading_says_what_happened(make_app):
    app = make_app()
    ids = seed(app)
    cache(app, ids[0])
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        await pilot.press("x")
        await pilot.press("x")
        notes = list(app._notifications)
        assert [n.message for n in notes] == [
            "Hidden — it won't show in Unread",
            "No longer hidden",
        ]
        assert not any(n.markup for n in notes)


async def test_leaving_mid_extraction_never_shows_the_stale_item(make_app):
    release = asyncio.Event()

    started = asyncio.Event()

    class SlowHttp(CountingHttp):
        async def get(self, url, **kwargs):
            started.set()
            await release.wait()
            return await super().get(url, **kwargs)

    app = make_app(http=SlowHttp({"https://x/0": GENERIC}))
    seed(app)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), timeout=5)  # the request is in flight
        await pilot.press("escape", "down")
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        reader = app.query_one(ReaderPane)
        assert "Summary of post 1" in reader.viewer.document.source
        assert "draft model" not in reader.viewer.document.source


async def test_reader_links_never_load_local_files(make_app, monkeypatch, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("# TOP SECRET")
    opened: list[str] = []
    monkeypatch.setattr(App, "open_url", lambda self, url, **kwargs: opened.append(url))
    app = make_app()
    cache(app, seed(app)[0], f"# Title\n\n[web](https://example.com/a) and [file]({secret})")
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        paragraph = app.query_one("MarkdownParagraph")
        for href in ("https://example.com/a", str(secret)):
            await paragraph.run_action(f"link({href!r})")
            await pilot.pause()
        assert opened == ["https://example.com/a"]
        assert "TOP SECRET" not in app.query_one(ReaderPane).viewer.document.source


async def test_reader_strips_terminal_escapes_from_the_body(make_app):
    app = make_app()
    cache(app, seed(app)[0], "# Title\n\nsafe \x1b]52;c;ZXZpbA==\x07text \x1b[31mred")
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        source = app.query_one(ReaderPane).viewer.document.source
        assert "\x1b" not in source and "safe text red" in source


async def test_http_client_is_closed_on_exit(make_app):
    closed: list[bool] = []

    class ClosingHttp(CountingHttp):
        async def aclose(self) -> None:
            closed.append(True)

    app = make_app(http=ClosingHttp())
    async with app.run_test(size=(180, 50)):
        assert isinstance(app.http, ClosingHttp)
    assert closed == [True]


@pytest.mark.parametrize("size", [(90, 40), (130, 40), (180, 50)])
def test_reader_snapshots(make_app, snap_compare, size):
    app = make_app()
    cache(app, seed(app)[0])
    assert snap_compare(app, terminal_size=size, run_before=open_first)
