from datetime import UTC, datetime, timedelta

from augury.agents.normalize import store_items
from augury.core.db.open import open_db
from augury.core.models import RawItem, Signals
from augury.tui.query import ItemFilter, count_new, fts_query, is_new, list_items

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def seed(paths):
    conn = open_db(paths, now=NOW)
    store_items(
        conn,
        [RawItem(source_id="hf-blog", url="https://x/old", title="Yesterday post")],
        now=NOW - timedelta(days=1),
    )
    store_items(
        conn,
        [
            RawItem(
                source_id="hf-papers",
                kind="paper",
                arxiv_id="2609.00001",
                url="https://huggingface.co/papers/2609.00001",
                title="Qwen3-30B-A3B beats GRPO",
                signals=Signals(upvotes=145),
            ),
            RawItem(
                source_id="hf-blog",
                url="https://x/a",
                title="Fast decoding",
                signals=Signals(upvotes7d=12),
            ),
            RawItem(source_id="hf-community", url="https://x/b", title="No signal post"),
        ],
        now=NOW,
    )
    return conn


def ids(rows) -> list[str]:
    return [r.title for r in rows]


def test_today_is_the_default_and_counts_everything(paths):
    rows, total = list_items(seed(paths), ItemFilter(), now=NOW)
    assert "Yesterday post" not in ids(rows) and len(rows) == 3 and total == 4
    assert len(list_items(seed(paths), ItemFilter(date="7d"), now=NOW)[0]) == 4


def test_search_matches_prefixes_and_jargon_tokens(paths):
    conn = seed(paths)
    assert ids(list_items(conn, ItemFilter(search="qwen3"), now=NOW)[0]) == [
        "Qwen3-30B-A3B beats GRPO"
    ]
    assert ids(list_items(conn, ItemFilter(search="Qwen3-30B-A3B"), now=NOW)[0]) == [
        "Qwen3-30B-A3B beats GRPO"
    ]
    assert list_items(conn, ItemFilter(search='"unbalanced'), now=NOW)[0] == []


def test_source_and_kind_filters(paths):
    conn = seed(paths)
    assert ids(list_items(conn, ItemFilter(kinds=frozenset({"paper"})), now=NOW)[0]) == [
        "Qwen3-30B-A3B beats GRPO"
    ]
    assert ids(list_items(conn, ItemFilter(sources=frozenset({"hf-community"})), now=NOW)[0]) == [
        "No signal post"
    ]


def test_popular_sort_puts_missing_signals_last(paths):
    rows, _ = list_items(seed(paths), ItemFilter(sort="popular"), now=NOW)
    assert ids(rows) == ["Qwen3-30B-A3B beats GRPO", "Fast decoding", "No signal post"]
    assert rows[1].popularity == 12  # upvotes7d preferred over upvotes


def test_show_filters_use_item_state(paths):
    conn = seed(paths)
    item = list_items(conn, ItemFilter(), now=NOW)[0][0]
    conn.execute(
        "INSERT INTO item_state (item_id, read_at, hidden, saved, updated_at)"
        " VALUES (?, ?, 0, 1, ?)",
        (item.id, NOW.isoformat(), NOW.isoformat()),
    )
    assert item.id not in [r.id for r in list_items(conn, ItemFilter(show="unread"), now=NOW)[0]]
    assert [r.id for r in list_items(conn, ItemFilter(show="saved"), now=NOW)[0]] == [item.id]
    conn.execute("UPDATE item_state SET hidden = 1 WHERE item_id = ?", (item.id,))
    assert item.id not in [r.id for r in list_items(conn, ItemFilter(show="all"), now=NOW)[0]]
    assert [r.id for r in list_items(conn, ItemFilter(show="hidden"), now=NOW)[0]] == [item.id]


def test_fts_query_quotes_every_token():
    assert fts_query('say "hi" -x') == '"say"* AND "hi"* AND "-x"*'
    assert fts_query("   ") is None


def seed_visits(paths):
    """Two items from before the last visit, three after it (one read, one hidden)."""
    conn = open_db(paths, now=NOW)
    before = [
        RawItem(source_id="hf-blog", url=f"https://x/old{i}", title=f"Old {i}") for i in range(2)
    ]
    store_items(conn, before, now=NOW - timedelta(days=2))
    after = [
        RawItem(source_id="hf-blog", url=f"https://x/new{i}", title=f"New {i}") for i in range(3)
    ]
    ids = store_items(conn, after, now=NOW).new_ids
    conn.execute(
        "INSERT INTO item_state (item_id, read_at, updated_at) VALUES (?, ?, ?)",
        (ids[1], NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO item_state (item_id, hidden, updated_at) VALUES (?, 1, ?)",
        (ids[2], NOW.isoformat()),
    )
    return conn


LAST_VISIT = NOW - timedelta(days=1)


def test_show_new_lists_unread_items_first_seen_after_the_last_visit(paths):
    conn = seed_visits(paths)
    everything = ItemFilter(date="all", show="new")
    assert ids(list_items(conn, everything, now=NOW, new_since=LAST_VISIT)[0]) == ["New 0"]
    # No last visit (the first launch): every unread, unhidden item is new.
    assert sorted(ids(list_items(conn, everything, now=NOW, new_since=None)[0])) == [
        "New 0",
        "Old 0",
        "Old 1",
    ]


def test_show_new_still_honours_the_other_filters(paths):
    conn = seed_visits(paths)
    rows = list_items(conn, ItemFilter(date="all", show="new", search="old"), now=NOW)[0]
    assert sorted(ids(rows)) == ["Old 0", "Old 1"]


def test_new_is_one_rule_in_sql_and_python(paths):
    conn = seed_visits(paths)
    for since in (None, LAST_VISIT, NOW, NOW - timedelta(days=3)):
        every_row = list_items(conn, ItemFilter(date="all", show="all"), now=NOW)[0]
        every_row += list_items(conn, ItemFilter(date="all", show="hidden"), now=NOW)[0]
        marked = sorted(r.id for r in every_row if is_new(r, since))
        listed = sorted(
            r.id
            for r in list_items(conn, ItemFilter(date="all", show="new"), now=NOW, new_since=since)[
                0
            ]
        )
        assert marked == listed and count_new(conn, since) == len(listed), since


def test_an_item_first_seen_in_the_same_second_as_the_visit_is_not_new(paths):
    conn = seed_visits(paths)
    since = NOW + timedelta(microseconds=500_000)  # first_seen is stored to the second
    assert count_new(conn, since) == 0
    assert count_new(conn, NOW - timedelta(seconds=1)) == 1


def test_show_new_ignores_the_date_chip_as_the_last_visit_is_its_own_floor(paths):
    conn = seed_visits(paths)  # "Old" items two days ago, "New" ones today
    since = NOW - timedelta(days=3)
    rows = list_items(conn, ItemFilter(show="new"), now=NOW, new_since=since)[0]  # Date: Today
    assert sorted(ids(rows)) == ["New 0", "Old 0", "Old 1"]
    assert len(rows) == count_new(conn, since)
