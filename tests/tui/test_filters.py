from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import ChoiceModal, PickerModal
from tests.tui.conftest import NOW


def seed(app):
    store_items(
        app.conn,
        [
            RawItem(
                source_id="hf-papers",
                kind="paper",
                arxiv_id="2609.00001",
                title="Qwen3-30B-A3B beats GRPO",
                url="https://huggingface.co/papers/2609.00001",
            ),
            RawItem(source_id="hf-blog", url="https://x/a", title="Fast decoding"),
            RawItem(source_id="hf-community", url="https://x/b", title="Community notes"),
        ],
        now=NOW,
    )


async def test_typing_filters_the_table_live(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", "q", "w", "e", "n")
        await pilot.pause()
        assert app.query_one(ItemsTable).row_count == 1
        assert app.mode == "SEARCH"


async def test_escape_returns_to_the_table(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("slash", "escape")
        assert app.focused is app.query_one(ItemsTable) and app.mode == "NORMAL"


async def test_sort_and_show_cycle_and_update_their_chips(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("s")
        assert (
            app.item_filter.sort == "popular"
            and app.query_one("#chip-sort", Chip).value == "Popular ↓"
        )
        await pilot.press("v")
        assert app.item_filter.show == "new" and app.query_one("#chip-show", Chip).value == "New"
        await pilot.press("v")
        assert app.item_filter.show == "all" and app.query_one("#chip-show", Chip).value == "All"


async def test_source_picker_filters_the_table(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("S")
        assert isinstance(app.screen, PickerModal)
        await pilot.press("space", "enter")  # tick the first source (hf-blog) and apply
        await pilot.pause()
        assert app.item_filter.sources == frozenset({"hf-blog"})
        assert app.query_one(ItemsTable).row_count == 1
        assert app.query_one("#chip-sources", Chip).value == "hf-blog"


async def test_escape_in_a_picker_changes_nothing(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("K", "space", "escape")
        await pilot.pause()
        assert app.item_filter.kinds == frozenset()


async def test_date_choice(make_app):
    app = make_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("D")
        assert isinstance(app.screen, ChoiceModal)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.item_filter.date == "7d" and app.query_one("#chip-date", Chip).value == "7 days"
