from datetime import UTC, datetime, timedelta

from augury.agents.normalize import store_items
from augury.core.db.open import open_db
from augury.core.models import RawItem, Signals
from augury.tui.query import ItemFilter, fts_query, list_items

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
