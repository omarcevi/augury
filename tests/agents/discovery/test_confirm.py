import sqlite3
from datetime import UTC, datetime

import pytest

from augury.agents.discovery.confirm import DuplicateCandidate, add_candidates, replace_with
from augury.agents.discovery.models import Candidate, SampleItem
from augury.core.db.discovery_repo import DiscoveryRepo
from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourcesRepo
from augury.core.models import RssRecipe, SitemapRecipe, Source

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def cand(url: str, name: str = "Example Engineering", dup: str | None = None) -> Candidate:
    c = Candidate.build(
        name=name,
        homepage="https://blog.example.com/",
        recipe=RssRecipe(feed_url=url),
        samples=[SampleItem(title="A", url="https://blog.example.com/a")],
        confidence=0.8,
        note="",
    )
    c.duplicate_of = dup
    return c


def test_confirmed_candidates_become_sources_and_the_choice_is_recorded(paths):
    conn = open_db(paths, now=NOW)
    DiscoveryRepo(conn).start("d-1", "example", "name", session_id="d-1", now=NOW)
    chosen = [cand("https://blog.example.com/feed.xml"), cand("https://x.example.com/f", dup="x")]
    added = add_candidates(conn, chosen, run_id="d-1", now=NOW)
    assert [s.id for s in added] == ["example-engineering"]
    record = SourcesRepo(conn).get("example-engineering")
    assert record is not None and record.source.added_via == "discovery"
    assert SourcesRepo(conn).discovery_run_id("example-engineering") == "d-1"
    run = DiscoveryRepo(conn).get("d-1")
    assert run is not None and run.chosen == [c.recipe_hash for c in chosen]


def test_re_discovery_replaces_the_recipe_of_the_same_source(paths):
    conn = open_db(paths, now=NOW)
    repo = SourcesRepo(conn)
    old = SitemapRecipe(sitemap_url="https://blog.example.com/sitemap.xml", include_pattern="/")
    repo.add(Source(id="blog", name="Blog", origin="user", recipe=old), now=NOW)
    assert replace_with(conn, "blog", cand("https://blog.example.com/feed.xml"), run_id=None)
    assert repo.get("blog").source.recipe.type == "rss"  # type: ignore[union-attr]
    repo.add(
        Source(id="other", name="O", origin="user", recipe=RssRecipe(feed_url="https://o/f")),
        now=NOW,
    )
    with pytest.raises(DuplicateCandidate, match="other"):
        replace_with(conn, "blog", cand("https://o/f"), run_id=None)


def test_an_interrupted_run_is_recorded(paths):
    conn = open_db(paths, now=NOW)
    DiscoveryRepo(conn).start("d-2", "x", "url", session_id="d-2", now=NOW)
    assert DiscoveryRepo(conn).mark_running_as_interrupted(now=NOW) == 1
    assert DiscoveryRepo(conn).get("d-2").status == "interrupted"  # type: ignore[union-attr]


# A fallback candidate is named after the query, so a long URL query gives a 63-character slug.
LONG_NAME = "https://engineering.example-company.com/blog/category/machine-learning/deep-dives/"


def test_long_names_get_distinct_ids_that_fit(paths):
    conn = open_db(paths, now=NOW)
    chosen = [cand(f"https://blog.example.com/{i}.xml", LONG_NAME) for i in range(3)]
    ids = [s.id for s in add_candidates(conn, chosen, run_id=None, now=NOW)]
    assert len(set(ids)) == 3 and all(len(i) <= 63 for i in ids)
    assert ids[1].endswith("-2") and ids[2].endswith("-3")
    assert all(SourcesRepo(conn).get(i) is not None for i in ids)


def test_a_failure_part_way_adds_nothing(paths, monkeypatch):
    conn = open_db(paths, now=NOW)
    DiscoveryRepo(conn).start("d-3", "example", "name", session_id="d-3", now=NOW)
    real_add, tried = SourcesRepo.add, []

    def add_then_fail(self, source, *, now, discovery_run_id=None):
        tried.append(source.id)
        if len(tried) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        real_add(self, source, now=now, discovery_run_id=discovery_run_id)

    monkeypatch.setattr(SourcesRepo, "add", add_then_fail)
    chosen = [cand("https://a.example.com/f", "A"), cand("https://b.example.com/f", "B")]
    with pytest.raises(sqlite3.OperationalError):
        add_candidates(conn, chosen, run_id="d-3", now=NOW)
    assert tried == ["a", "b"]
    assert [r.source.id for r in SourcesRepo(conn).list_all() if r.source.origin == "user"] == []
    assert DiscoveryRepo(conn).get("d-3").chosen == []  # type: ignore[union-attr]


def test_a_homepage_off_the_recipe_host_is_never_stored(paths):
    conn = open_db(paths, now=NOW)
    c = cand("https://blog.example.com/feed.xml")
    c.homepage = "http://169.254.169.254/latest/meta-data/"  # e.g. read back from a run's JSON
    [source] = add_candidates(conn, [c], run_id=None, now=NOW)
    record = SourcesRepo(conn).get(source.id)
    assert record is not None and record.source.homepage == "https://blog.example.com/"
