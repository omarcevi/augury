from datetime import UTC, datetime, timedelta

from augury.agents.normalize import store_items
from augury.core.db.open import open_db
from augury.core.db.state_repo import StateRepo
from augury.core.models import RawItem

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def setup(paths) -> tuple[StateRepo, str]:
    conn = open_db(paths, now=NOW)
    item_id = store_items(
        conn, [RawItem(source_id="hf-blog", url="https://x/a", title="A")], now=NOW
    ).new_ids[0]
    return StateRepo(conn), item_id


def test_toggles_flip_and_log_every_change(paths):
    repo, item_id = setup(paths)
    assert repo.toggle(item_id, "liked", now=NOW) is True
    assert repo.toggle(item_id, "liked", now=NOW) is False
    assert repo.toggle(item_id, "hidden", now=NOW) is True
    assert repo.interactions(item_id) == ["like", "unlike", "hide"]
    state = repo.get(item_id)
    assert (state.liked, state.hidden) == (False, True)


def test_first_open_time_is_kept(paths):
    repo, item_id = setup(paths)
    repo.mark_opened(item_id, now=NOW)
    repo.mark_opened(item_id, now=NOW + timedelta(days=1))
    assert repo.get(item_id).read_at == NOW
    assert repo.interactions(item_id) == ["open", "open"]


def test_progress_never_goes_backwards_and_is_clamped(paths):
    repo, item_id = setup(paths)
    repo.set_progress(item_id, 0.6, now=NOW)
    repo.set_progress(item_id, 0.2, now=NOW)
    assert repo.get(item_id).read_progress == 0.6
    repo.set_progress(item_id, 7.0, now=NOW)
    assert repo.get(item_id).read_progress == 1.0


def test_unknown_items_have_a_blank_state(paths):
    repo, _ = setup(paths)
    assert repo.get("web:missing").read_at is None
