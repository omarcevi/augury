from textual.widgets import Static

from augury.tui.widgets.items_table import ItemsTable
from tests.rag.helpers import add_items
from tests.tui.conftest import NOW, until


def _header(app) -> str:
    return str(app.query_one("#reader-header", Static).content)


async def test_the_reader_names_the_sources_that_also_cover_an_item(make_app):
    app = make_app()
    [blog] = add_items(app.conn, [("Sparse attention", "x")], now=NOW)
    [paper] = add_items(
        app.conn, [("Sparse attention paper", "y")], source_id="hf-papers", kind="paper", now=NOW
    )
    [alone] = add_items(app.conn, [("Tomato soup", "z")], source_id="hf-community", now=NOW)
    app.conn.execute("UPDATE items SET cluster_id = ? WHERE id IN (?, ?)", (blog, blog, paper))
    async with app.run_test(size=(160, 40)) as pilot:
        table = app.query_one(ItemsTable)
        table.select_key(blog)  # a preview: local data only
        await until(pilot, lambda: "Also covered by: hf-papers" in _header(app))
        table.select_key(alone)
        await until(pilot, lambda: "Tomato soup" in _header(app))
        assert "Also covered by" not in _header(app)
        table.select_key(paper)
        await pilot.press("enter")  # opened, the header keeps it
        await until(
            pilot, lambda: app.reading_id == paper and "Also covered by: hf-blog" in _header(app)
        )
