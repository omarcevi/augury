from textual.app import App

from augury.agents.normalize import store_items
from augury.core.db.state_repo import StateRepo
from augury.core.models import RawItem
from augury.tui.widgets.items_table import ItemsTable
from tests.tui.conftest import NOW


def seed(app):
    store_items(
        app.conn,
        [RawItem(source_id="hf-blog", url=f"https://x/{i}", title=f"Post {i}") for i in range(2)],
        now=NOW,
    )


async def test_like_shows_a_star_and_survives_a_restart(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("l")
        assert "★" in app.query_one(ItemsTable).get_row_at(0)[1].plain
    again = make_app()
    async with again.run_test(size=(140, 40)):
        assert "★" in again.query_one(ItemsTable).get_row_at(0)[1].plain


async def test_hide_removes_the_row_from_the_digest(make_app):
    app = make_app()
    seed(app)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("x")
        assert app.query_one(ItemsTable).row_count == 1


async def test_open_in_browser_uses_the_item_url_and_marks_it_opened(make_app):
    app = make_app()
    seed(app)
    opened: list[str] = []
    app.open_url = lambda url, **kwargs: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("o")
        assert opened and opened[0].startswith("https://x/")
        assert app.query_one(ItemsTable).get_row_at(0)[0].plain == "◐"


async def test_blocked_url_scheme_never_opens_and_never_marks_opened(make_app, monkeypatch):
    app = make_app()
    result = store_items(
        app.conn, [RawItem(source_id="hf-blog", url="javascript:alert(1)", title="Evil")], now=NOW
    )
    item_id = result.new_ids[0]
    calls: list[str] = []
    monkeypatch.setattr(App, "open_url", lambda self, url, **kwargs: calls.append(url))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("o")
        assert calls == []
    assert StateRepo(app.conn).get(item_id).read_at is None
