from datetime import UTC, datetime, timedelta

import pytest

from augury.agents.normalize import normalize, store_items
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.open import open_db
from augury.core.models import RawItem, Signals

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


def raw(**overrides) -> RawItem:
    base = {"source_id": "hf-blog", "url": "https://huggingface.co/blog/a", "title": "A post"}
    return RawItem.model_validate(base | overrides)


def test_normalize_cleans_title_and_builds_ids():
    n = normalize(raw(title="  Red \x1b[31mAlert\x1b[0m\n  now  ", summary="s\x00"))
    assert n.title == "Red Alert now" and n.summary == "s"
    assert n.id.startswith("web:")
    paper = normalize(
        raw(kind="paper", url="https://huggingface.co/papers/2609.24984", arxiv_id="2609.24984")
    )
    assert paper.id == "arxiv:2609.24984"


def test_empty_title_is_rejected():
    with pytest.raises(ValueError):
        normalize(raw(title="\x1b[0m  "))


def test_article_linking_a_paper_records_arxiv_id_but_keeps_web_identity():
    n = normalize(raw(summary="Code for https://arxiv.org/abs/2609.24984"))
    assert n.arxiv_id == "2609.24984" and n.id.startswith("web:")


def test_same_day_repeat_is_not_new_and_not_counted_twice(paths):
    conn = open_db(paths, now=NOW)
    first = store_items(conn, [raw()], now=NOW)
    again = store_items(conn, [raw()], now=NOW + timedelta(hours=2))
    assert (first.new, again.new) == (1, 0)
    assert ItemsRepo(conn).get(first.new_ids[0]).times_seen == 1  # type: ignore[union-attr]


def test_next_day_increments_times_seen(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw()], now=NOW).new_ids[0]
    store_items(conn, [raw()], now=NOW + timedelta(days=1))
    assert ItemsRepo(conn).get(item_id).times_seen == 2  # type: ignore[union-attr]


def test_tracking_variants_merge_into_one_item(paths):
    conn = open_db(paths, now=NOW)
    result = store_items(
        conn,
        [
            raw(url="https://huggingface.co/blog/a?utm_source=x"),
            raw(url="https://huggingface.co/blog/a/"),
        ],
        now=NOW,
    )
    assert result.new == 1 and ItemsRepo(conn).count() == 1


def test_empty_api_summary_never_clobbers_an_existing_summary(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw(summary="Opening paragraph.")], now=NOW).new_ids[0]
    store_items(conn, [raw(summary="")], now=NOW + timedelta(days=1))
    assert ItemsRepo(conn).get(item_id).summary == "Opening paragraph."  # type: ignore[union-attr]


def test_old_paper_is_flagged(paths):
    conn = open_db(paths, now=NOW)
    item = raw(
        kind="paper",
        url="https://huggingface.co/papers/2412.20138",
        arxiv_id="2412.20138",
        published_at=datetime(2024, 12, 28, tzinfo=UTC),
    )
    item_id = store_items(conn, [item], now=NOW).new_ids[0]
    assert ItemsRepo(conn).get(item_id).is_old is True  # type: ignore[union-attr]


def test_signals_are_snapshotted_per_local_day(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw(signals=Signals(upvotes=5), rank=3)], now=NOW).new_ids[0]
    store_items(conn, [raw(signals=Signals(upvotes=9), rank=1)], now=NOW + timedelta(minutes=5))
    sql = "SELECT upvotes, rank FROM signals WHERE item_id = ?"
    rows = conn.execute(sql, (item_id,)).fetchall()
    assert [tuple(r) for r in rows] == [(9, 1)]


def test_items_without_title_are_skipped_not_fatal(paths):
    conn = open_db(paths, now=NOW)
    result = store_items(conn, [raw(), raw(url="https://x/b", title=" ")], now=NOW)
    assert (result.seen, result.skipped) == (1, 1)


def test_backfilled_published_at_recomputes_is_old(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw()], now=NOW).new_ids[0]
    old_date = NOW - timedelta(days=200)
    store_items(conn, [raw(published_at=old_date)], now=NOW + timedelta(days=5))
    item = ItemsRepo(conn).get(item_id)
    assert item.is_old is True  # type: ignore[union-attr]
    assert item.first_seen == NOW  # type: ignore[union-attr]


def test_backfilled_authors_when_none_recorded(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw()], now=NOW).new_ids[0]
    store_items(conn, [raw(authors=["Ada Lovelace"])], now=NOW + timedelta(days=1))
    item = ItemsRepo(conn).get(item_id)
    assert item.authors == ["Ada Lovelace"]  # type: ignore[union-attr]


def test_existing_authors_are_not_overwritten(paths):
    conn = open_db(paths, now=NOW)
    item_id = store_items(conn, [raw(authors=["Ada Lovelace"])], now=NOW).new_ids[0]
    store_items(conn, [raw(authors=["Alan Turing"])], now=NOW + timedelta(days=1))
    item = ItemsRepo(conn).get(item_id)
    assert item.authors == ["Ada Lovelace"]  # type: ignore[union-attr]
