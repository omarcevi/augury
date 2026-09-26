from dataclasses import replace
from datetime import UTC, datetime, timedelta

from augury.agents.normalize import store_items
from augury.core.db.open import open_db
from augury.core.db.state_repo import StateRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import RawItem, Signals, TriageResult
from augury.tui.query import (
    ItemFilter,
    count_new,
    fts_query,
    get_item_row,
    is_new,
    list_items,
    tag_options,
    triage_hidden,
)

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


# --- M2 · the ranked digest and triage (F27) --------------------------------------------------


def _ids(conn) -> dict[str, str]:
    rows, _ = list_items(conn, ItemFilter(date="all", show="all"), now=NOW)
    return {r.title: r.id for r in rows}


def _triage(conn, results: list[TriageResult]) -> None:
    TriageRepo(conn).save_all(results, run_id="r", model="m", prompt_version=1)


def test_score_sort_puts_unranked_items_last(paths):
    conn = seed(paths)
    ids = _ids(conn)
    conn.execute(
        "INSERT INTO digests (day, item_id, position, final_score, breakdown_json)"
        " VALUES ('2026-09-25', ?, 1, 0.9, '{}')",
        (ids["Fast decoding"],),
    )
    rows, _ = list_items(conn, ItemFilter(sort="score"), now=NOW)
    assert rows[0].title == "Fast decoding" and rows[0].score == 0.9
    assert all(r.score is None for r in rows[1:])


def test_the_default_sort_is_score_and_unranked_rows_fall_back_to_newest(paths):
    conn = seed(paths)
    assert ItemFilter().sort == "score"
    by_score = ids(list_items(conn, ItemFilter(), now=NOW)[0])
    assert by_score == ids(list_items(conn, ItemFilter(sort="newest"), now=NOW)[0])


def test_the_latest_digest_row_is_the_one_joined(paths):
    conn = seed(paths)
    item = _ids(conn)["Fast decoding"]
    conn.executemany(
        "INSERT INTO digests (day, item_id, position, final_score, breakdown_json)"
        " VALUES (?, ?, 1, ?, '{}')",
        [("2026-09-24", item, 0.2), ("2026-09-25", item, 0.7)],
    )
    rows, _ = list_items(conn, ItemFilter(date="all", show="all"), now=NOW)
    assert [r.score for r in rows if r.id == item] == [0.7]  # one row per item, the latest day
    assert get_item_row(conn, item, now=NOW).score == 0.7  # type: ignore[union-attr]


def test_promo_and_thin_leave_the_top_views_and_are_counted(paths):
    conn = seed(paths)
    ids = _ids(conn)
    _triage(
        conn,
        [
            TriageResult(item_id=ids["Fast decoding"], relevance=3, flags=["promo"]),
            TriageResult(item_id=ids["No signal post"], relevance=1, flags=["off_topic", "thin"]),
        ],
    )
    rows, _ = list_items(conn, ItemFilter(), now=NOW)
    assert [r.title for r in rows] == ["Qwen3-30B-A3B beats GRPO"]
    hidden = triage_hidden(conn, ItemFilter(), now=NOW)
    assert hidden.total == 2 and dict(hidden.by_flag) == {"promo": 1, "thin": 1}
    assert len(list_items(conn, ItemFilter(show_triage_hidden=True), now=NOW)[0]) == 3
    assert triage_hidden(conn, ItemFilter(show="saved"), now=NOW).total == 0


def test_the_list_the_hidden_count_and_the_tags_agree_about_a_view(paths):
    # One set of conditions (_conditions) decides what a view contains, for all three.
    conn = seed(paths)
    ids = _ids(conn)
    _triage(
        conn,
        [
            TriageResult(item_id=ids["Fast decoding"], relevance=3, flags=["promo"], tags=["x"]),
            TriageResult(item_id=ids["Yesterday post"], relevance=3, flags=["thin"], tags=["x"]),
            TriageResult(item_id=ids["No signal post"], relevance=5, tags=["x", "y"]),
        ],
    )
    tagged_x = {ids["Fast decoding"], ids["Yesterday post"], ids["No signal post"]}
    for f in (
        ItemFilter(),
        ItemFilter(date="all"),
        ItemFilter(sources=frozenset({"hf-blog"})),
        ItemFilter(search="post", date="all"),
        ItemFilter(show="new", date="all"),
    ):
        expanded = replace(f, show_triage_hidden=True)
        shown = {r.id for r in list_items(conn, f, now=NOW)[0]}
        everything = {r.id for r in list_items(conn, expanded, now=NOW)[0]}
        assert triage_hidden(conn, f, now=NOW).total == len(everything - shown), f
        assert dict(tag_options(conn, f, now=NOW)).get("x", 0) == len(shown & tagged_x), f
        assert dict(tag_options(conn, expanded, now=NOW)).get("x", 0) == len(
            everything & tagged_x
        ), f


def test_tag_filter_and_tag_options(paths):
    conn = seed(paths)
    ids = _ids(conn)
    _triage(
        conn,
        [
            TriageResult(item_id=ids["Fast decoding"], relevance=5, tags=["agents", "rag"]),
            TriageResult(item_id=ids["No signal post"], relevance=5, tags=["agents"]),
        ],
    )
    assert tag_options(conn, ItemFilter(), now=NOW) == [("agents", 2), ("rag", 1)]
    rag, _ = list_items(conn, ItemFilter(tags=frozenset({"rag"})), now=NOW)
    assert [r.title for r in rag] == ["Fast decoding"] and rag[0].tags == ("agents", "rag")
    # The picker's counts ignore its own filter, so the other tags stay pickable.
    assert tag_options(conn, ItemFilter(tags=frozenset({"rag"})), now=NOW) == [
        ("agents", 2),
        ("rag", 1),
    ]


def test_a_source_counts_as_liked_only_after_a_like_from_it(paths):
    conn = seed(paths)
    ids = _ids(conn)
    assert not any(r.source_liked for r in list_items(conn, ItemFilter(), now=NOW)[0])
    StateRepo(conn).toggle(ids["Yesterday post"], "liked", now=NOW)  # hf-blog
    rows, _ = list_items(conn, ItemFilter(), now=NOW)
    assert {r.source_id: r.source_liked for r in rows} == {
        "hf-papers": False,
        "hf-blog": True,
        "hf-community": False,
    }
    assert get_item_row(conn, ids["Fast decoding"], now=NOW).source_liked  # type: ignore[union-attr]
    StateRepo(conn).toggle(ids["Yesterday post"], "hidden", now=NOW)  # a hidden like is no like
    assert not any(r.source_liked for r in list_items(conn, ItemFilter(), now=NOW)[0])
