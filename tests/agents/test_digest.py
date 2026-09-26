from datetime import UTC, datetime, timedelta

import pytest

from augury.agents.normalize import store_items
from augury.agents.rank import build_digest
from augury.core.clock import local_day
from augury.core.config import RankingConfig
from augury.core.db.digest_repo import DigestRepo, DigestRow
from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.state_repo import StateRepo
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import RawItem, RssRecipe, Signals, Source, TriageResult

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
TODAY = local_day(NOW)


def paper(n: int, upvotes: int) -> RawItem:
    arxiv_id = f"2609.{n:05d}"
    return RawItem(
        source_id="hf-papers",
        kind="paper",
        arxiv_id=arxiv_id,
        title=f"Paper {n}",
        url=f"https://huggingface.co/papers/{arxiv_id}",
        signals=Signals(upvotes=upvotes),
    )


def blog(slug: str, upvotes7d: int | None = None) -> RawItem:
    return RawItem(
        source_id="hf-blog",
        url=f"https://x/{slug}",
        title=slug,
        signals=Signals(upvotes7d=upvotes7d),
    )


def triaged(conn, relevance: dict[str, int], flags: dict[str, list[str]] | None = None) -> None:
    results = [
        TriageResult(item_id=i, relevance=r, flags=(flags or {}).get(i, []))
        for i, r in relevance.items()
    ]
    TriageRepo(conn).save_all(results, run_id="r1", model="fake/fake-model", prompt_version=1)


def digest(conn, *, ai: bool = True) -> list[DigestRow]:
    build_digest(conn, TODAY, now=NOW, config=RankingConfig(), ai=ai)
    return DigestRepo(conn).for_day(TODAY)


def test_relevance_popularity_and_recency_order_the_day(paths):
    conn = open_db(paths, now=NOW)
    a, b = store_items(conn, [paper(1, 20), paper(2, 10)], now=NOW).new_ids
    [post] = store_items(conn, [blog("post", upvotes7d=5)], now=NOW).new_ids
    triaged(conn, {a: 9, b: 2})
    rows = digest(conn)
    assert [r.item_id for r in rows] == [a, post, b]
    assert [r.position for r in rows] == [1, 2, 3]
    assert rows[0].final_score == pytest.approx((0.5 * 0.9 + 0.25 * 0.75 + 0.15 * 1.0) / 0.9)
    assert rows[1].breakdown["missing"] == ["rel", "nov"]  # untriaged: pop + rec only


def test_popularity_is_a_percentile_within_each_source(paths):
    conn = open_db(paths, now=NOW)
    top_paper, _ = store_items(conn, [paper(1, 200), paper(2, 100)], now=NOW).new_ids
    _, top_blog = store_items(conn, [blog("one", 1), blog("two", 2)], now=NOW).new_ids
    rows = {r.item_id: r for r in digest(conn)}
    assert rows[top_paper].breakdown["terms"]["pop"] == 0.75
    assert rows[top_blog].breakdown["terms"]["pop"] == 0.75  # 2 upvotes7d rank like 200 upvotes


def test_popularity_ignores_items_older_than_thirty_days(paths):
    conn = open_db(paths, now=NOW)
    store_items(conn, [paper(9, 1000)], now=NOW - timedelta(days=31))
    top, _ = store_items(conn, [paper(1, 200), paper(2, 100)], now=NOW).new_ids
    rows = {r.item_id: r for r in digest(conn)}
    assert rows[top].breakdown["terms"]["pop"] == 0.75  # the old 1000 isn't in the population


def test_a_source_without_signals_ranks_on_what_it_has(paths):
    conn = open_db(paths, now=NOW)
    feed = Source(
        id="some-blog",
        name="Some blog",
        origin="user",
        recipe=RssRecipe(feed_url="https://some.blog/feed"),
    )
    SourcesRepo(conn).add(feed, now=NOW)
    raw = RawItem(source_id="some-blog", url="https://some.blog/p", title="P")
    [item] = store_items(conn, [raw], now=NOW).new_ids
    triaged(conn, {item: 6})
    [row] = digest(conn)
    assert row.breakdown["missing"] == ["pop", "nov"]
    assert row.final_score == pytest.approx((0.5 * 0.6 + 0.15 * 1.0) / 0.65)


def test_the_digest_holds_only_items_first_seen_that_day(paths):
    conn = open_db(paths, now=NOW)
    store_items(conn, [blog("old")], now=NOW - timedelta(days=1))
    [new] = store_items(conn, [blog("new")], now=NOW).new_ids
    assert [r.item_id for r in digest(conn)] == [new]


def test_an_item_already_in_an_earlier_digest_does_not_reappear(paths):
    conn = open_db(paths, now=NOW)
    [item] = store_items(conn, [blog("a")], now=NOW).new_ids
    DigestRepo(conn).replace_day(TODAY - timedelta(days=1), [(item, 1, 0.5, "{}")])
    assert digest(conn) == []


def test_rebuilding_a_day_replaces_its_rows(paths):
    conn = open_db(paths, now=NOW)
    store_items(conn, [blog("a")], now=NOW)
    digest(conn)
    assert len(digest(conn)) == 1


def test_items_hidden_by_triage_are_ranked_and_counted(paths):
    conn = open_db(paths, now=NOW)
    ad, ok = store_items(conn, [blog("ad"), blog("ok")], now=NOW).new_ids
    triaged(conn, {ad: 3, ok: 8}, {ad: ["promo"]})
    stats = build_digest(conn, TODAY, now=NOW, config=RankingConfig(), ai=True)
    assert (stats.items, stats.hidden, stats.without_relevance) == (2, 1, 0)


# Pre-flight, user decision 2026-09-26: no key → your likes + recency; no likes → pop + recency.
def test_without_a_key_or_likes_the_day_ranks_by_popularity_and_recency(paths):
    conn = open_db(paths, now=NOW)
    low, high = store_items(conn, [paper(1, 10), paper(2, 90)], now=NOW).new_ids
    rows = digest(conn, ai=False)
    assert [r.item_id for r in rows] == [high, low]
    assert rows[0].breakdown["mode"] == "cold" and set(rows[0].breakdown["terms"]) == {"pop", "rec"}


def titled(slug: str, title: str, upvotes7d: int | None = None) -> RawItem:
    return RawItem(
        source_id="hf-blog",
        url=f"https://x/{slug}",
        title=title,
        signals=Signals(upvotes7d=upvotes7d),
    )


def test_without_a_key_items_like_your_likes_outrank_others_of_the_same_age(paths):
    conn = open_db(paths, now=NOW)
    old = store_items(
        conn,
        [titled("a", "Latent diffusion tricks"), titled("b", "Diffusion transformers")],
        now=NOW - timedelta(days=2),
    ).new_ids
    for item_id in old:
        StateRepo(conn).toggle(item_id, "liked", now=NOW)
    other, match = store_items(
        conn, [titled("c", "A tokenizer primer", 50), titled("d", "A diffusion primer")], now=NOW
    ).new_ids
    rows = digest(conn, ai=False)
    assert [r.item_id for r in rows] == [match, other]  # popularity doesn't count here
    assert rows[0].breakdown["mode"] == "likes" and rows[0].breakdown["matched"] == ["diffusion"]
    assert set(rows[0].breakdown["terms"]) == {"topic", "source", "rec"}
