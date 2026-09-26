from datetime import timedelta

from augury.agents.normalize import store_items
from augury.core.models import RawItem
from augury.tui.query import ItemFilter
from augury.tui.widgets.items_table import ItemsTable, humanize_age
from tests.tui.conftest import NOW

HOSTILE = "Evil [link=https://x.y]click[/link] title"
CJK = "大型语言模型的推测解码：一种非常长的标题 🚀🚀🚀 " * 4  # noqa: RUF001


def seed(app, titles, now=NOW):
    store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url=f"https://x/{i}", title=t) for i, t in enumerate(titles)],
        now=now,
    )


async def test_table_lists_todays_items_with_counts(make_app):
    app = make_app()
    seed(app, ["One", "Two"])
    async with app.run_test(size=(120, 30)):
        table = app.query_one(ItemsTable)
        assert table.row_count == 2
        assert app.query_one("#items-pane").border_title == "Items (2/2)"


async def test_hostile_title_renders_literally(make_app):
    app = make_app()
    seed(app, [HOSTILE])
    async with app.run_test(size=(120, 30)):
        table = app.query_one(ItemsTable)
        title_cell = table.get_row_at(0)[1]
        assert "[link=" in title_cell.plain


async def test_long_cjk_emoji_title_stays_one_line(make_app):
    app = make_app()
    seed(app, [CJK])
    async with app.run_test(size=(120, 30)):
        table = app.query_one(ItemsTable)
        assert table.get_row_at(0)[1].no_wrap
        assert all(row.height == 1 for row in table.rows.values())


async def test_row_without_published_date_uses_first_seen(make_app):
    app = make_app()
    seed(app, ["Undated"], now=NOW - timedelta(hours=3))
    async with app.run_test(size=(120, 30)):
        app.item_filter = ItemFilter(date="7d")
        app.reload_items()
        table = app.query_one(ItemsTable)
        assert table.get_cell(next(iter(table.rows_by_key)), "age").plain == "3h"


async def test_empty_state_is_explained(make_app):
    app = make_app()
    async with app.run_test(size=(120, 30)):
        assert app.query_one("#empty").display is True
        assert app.query_one(ItemsTable).display is False


def test_humanize_age():
    assert [
        humanize_age(timedelta(minutes=5)),
        humanize_age(timedelta(hours=5)),
        humanize_age(timedelta(days=3)),
        humanize_age(timedelta(days=20)),
        humanize_age(timedelta(days=90)),
    ] == ["5m", "5h", "3d", "2w", "3mo"]


def test_items_snapshot(make_app, snap_compare):
    app = make_app()
    seed(app, ["Qwen3-30B-A3B beats GRPO", HOSTILE, CJK])
    assert snap_compare(app, terminal_size=(120, 30))
