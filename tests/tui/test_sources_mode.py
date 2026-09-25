from textual.widgets import ContentSwitcher, Input

from augury.agents.normalize import store_items
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.state_repo import StateRepo
from augury.core.models import RawItem, RssRecipe, Source
from augury.tui.widgets.confirm_modal import ConfirmModal
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import ChoiceModal, PickerModal
from augury.tui.widgets.source_detail import SourceDetail
from augury.tui.widgets.sources_table import SourcesTable
from tests.adapters.test_probe import ORIGIN as BLOG
from tests.adapters.test_probe import PAGE_WITH_LINK
from tests.adapters.test_rss import FEED
from tests.helpers import CountingHttp
from tests.tui.conftest import NOW

FEED_URL = f"{BLOG}/posts.xml"


def add_user_source(app) -> None:
    SourcesRepo(app.conn).add(
        Source(
            id="example-blog", name="Example", origin="user", recipe=RssRecipe(feed_url=FEED_URL)
        ),
        now=NOW,
    )


def seed_item(app, *, source_id: str = "hf-blog") -> str:
    return store_items(
        app.conn, [RawItem(source_id=source_id, url="https://x/a", title="A")], now=NOW
    ).new_ids[0]


async def test_2_shows_sources_and_1_goes_back(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2")
        assert app.query_one(ContentSwitcher).current == "sources-view"
        assert app.query_one(SourcesTable).row_count == 3
        await pilot.press("1")
        assert app.query_one(ContentSwitcher).current == "main"


async def test_add_a_blog_by_url_inside_the_tui(make_app):
    app = make_app(http=CountingHttp({f"{BLOG}/": PAGE_WITH_LINK, FEED_URL: FEED}))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "plus")
        app.query_one("#add-url", Input).value = f"{BLOG}/"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # confirm the highlighted candidate
        await pilot.pause()
        assert SourcesRepo(app.conn).get("example-blog") is not None
        assert app.query_one(SourcesTable).row_count == 4


async def test_e_toggles_enabled(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "e")
        assert SourcesRepo(app.conn).get("hf-blog").source.enabled is False  # type: ignore[union-attr]


async def test_e_twice_toggles_the_same_source_back(make_app):
    # Regression: refresh_view() rebuilds the table (DataTable.clear() resets the cursor to
    # row 0), so a second "e" without moving the cursor must still hit the same source --
    # not whichever source now sorts first.
    app = make_app()
    add_user_source(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "down", "down", "down", "e", "e")
        record = SourcesRepo(app.conn).get("example-blog")
        assert record is not None and record.source.enabled is True
        assert SourcesRepo(app.conn).get("hf-blog").source.enabled is True  # type: ignore[union-attr]


async def test_builtin_sources_cannot_be_removed(make_app):
    app = make_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "d")
        assert not isinstance(app.screen, ConfirmModal)
        assert SourcesRepo(app.conn).get("hf-blog") is not None


async def test_user_source_is_removed_after_confirmation(make_app):
    app = make_app()
    add_user_source(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "down", "down", "down", "d")
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        assert SourcesRepo(app.conn).get("example-blog") is None


async def test_t_test_fetches_and_shows_samples(make_app):
    app = make_app(http=CountingHttp({FEED_URL: FEED}))
    add_user_source(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("2", "down", "down", "down", "t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "First & best" in app.query_one(SourceDetail).text_content


async def test_item_actions_and_pickers_are_inert_in_sources_view(make_app):
    # Regression: app-level l/b/x/o and the picker/cycle/search keys aren't gated by mode or
    # view, so with the Sources view showing they used to fall through to the hidden
    # ItemsTable/reader and act on whatever item was last selected/read.
    app = make_app()
    item_id = seed_item(app)
    opened: list[str] = []
    app.open_url = lambda url, **kwargs: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(160, 40)) as pilot:
        before = StateRepo(app.conn).get(item_id)
        filter_before = app.item_filter
        await pilot.press("2")

        await pilot.press("l", "b", "x", "o")
        await pilot.pause()
        assert StateRepo(app.conn).get(item_id) == before
        assert opened == []

        await pilot.press("S")
        assert not isinstance(app.screen, PickerModal)
        await pilot.press("K")
        assert not isinstance(app.screen, PickerModal)
        await pilot.press("D")
        assert not isinstance(app.screen, ChoiceModal)

        await pilot.press("s", "v", "slash")
        assert app.item_filter == filter_before
        assert app.mode == "SOURCES"  # "/" never focused search / left Sources

        # Still exactly where we were: none of the above left the Sources view.
        assert app.query_one(ContentSwitcher).current == "sources-view"

        await pilot.press("1")
        await pilot.press("l")
        await pilot.pause()
        assert StateRepo(app.conn).get(item_id).liked is True


async def test_switching_to_sources_and_back_keeps_the_reader_open(make_app):
    app = make_app()
    item_id = seed_item(app)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.open_item(item_id)
        await pilot.pause()
        assert app.reading_id == item_id

        await pilot.press("2")
        assert app.reading_id == item_id  # untouched while Sources is showing

        await pilot.press("1")
        await pilot.pause()
        assert app.reading_id == item_id  # intact, not discarded
        assert app.mode == "READ"
        assert app.screen.has_class("reading")

        await pilot.pause(0.4)
        await app.workers.wait_for_complete()


async def test_removing_the_open_items_source_closes_the_reader_gracefully(make_app):
    app = make_app()
    add_user_source(app)
    item_id = seed_item(app, source_id="example-blog")
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.open_item(item_id)
        await pilot.pause()
        assert app.reading_id == item_id

        await pilot.press("2", "down", "down", "down", "d")
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await pilot.pause()

        assert app.reading_id is None  # closed gracefully, no crash
        assert not app.screen.has_class("reading")
        assert app.query_one(ItemsTable).row_count == 0  # its item is gone too; the view refreshed

        await pilot.pause(0.4)
        await app.workers.wait_for_complete()
