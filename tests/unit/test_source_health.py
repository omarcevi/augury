from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourceRecord, SourcesRepo, health_state
from augury.core.models import FetchState, RssRecipe, Source
from augury.sources.health import can_rediscover, health_note, rediscover_target

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
FEED = "https://blog.example.com/feed.xml"


def _source(
    recipe=None,
    homepage: str = "https://blog.example.com/",
    origin: Literal["builtin", "user"] = "user",
) -> Source:
    return Source(
        id="blog",
        name="Blog",
        homepage=homepage,
        origin=origin,
        recipe=recipe or RssRecipe(feed_url=FEED),
    )


@pytest.mark.parametrize(
    ("failures", "last_ok", "expected"),
    [
        (0, None, "never"),
        (0, NOW, "ok"),
        (1, NOW, "degraded"),
        (2, None, "degraded"),
        (3, NOW, "broken"),
        (9, None, "broken"),
    ],
)
def test_health_state_machine(failures, last_ok, expected):
    assert health_state(failures, last_ok) == expected


def test_notes_say_what_a_state_means():
    ok = SourceRecord(_source(), NOW, None, 0)
    assert health_note(ok) is None
    degraded = SourceRecord(_source(), NOW, "x", 1)
    assert "degraded: the last fetch failed" in (health_note(degraded) or "")
    assert "2 more" in (health_note(degraded) or "")
    broken = SourceRecord(_source(), None, "x", 4)
    assert (health_note(broken) or "").startswith("broken: the last 4 fetches failed")
    assert "never worked" in (health_note(broken) or "")


def test_rediscovery_starts_at_the_homepage_or_the_recipe_site():
    assert rediscover_target(SourceRecord(_source(), None, None, 3)) == "https://blog.example.com/"
    feed = RssRecipe(feed_url="https://labs.example.com/research/feed.xml")
    no_home = SourceRecord(_source(feed, homepage=""), None, None, 3)
    assert rediscover_target(no_home) == "https://labs.example.com/"
    assert can_rediscover(no_home)
    assert not can_rediscover(SourceRecord(_source(origin="builtin"), None, None, 3))


def test_duplicates_are_found_by_any_recipe_url(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    repo.add(_source(), now=NOW)
    other = RssRecipe(feed_url="https://x.example.com/rss")
    repo.add(_source(other).model_copy(update={"id": "x"}), now=NOW, discovery_run_id="d-1")
    # sitemap and html_listing recipes keep their URL under another key (Tasks 31-32)
    repo.conn.execute(
        "INSERT INTO sources (id, name, origin, recipe_type, recipe_json, created_at)"
        " VALUES ('sm', 'Sm', 'user', 'sitemap', ?, '2026-09-25T09:00:00+00:00')",
        ('{"type": "sitemap", "sitemap_url": "https://sm.example.com/sitemap.xml"}',),
    )
    assert repo.find_by_recipe_url(FEED) == "blog"
    assert repo.find_by_recipe_url("https://x.example.com/rss") == "x"
    assert repo.find_by_recipe_url("https://sm.example.com/sitemap.xml") == "sm"
    assert repo.find_by_recipe_url("https://nope.example.com/") is None
    assert repo.discovery_run_id("x") == "d-1" and repo.discovery_run_id("blog") is None


def test_replacing_a_recipe_keeps_the_source_and_resets_its_health(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    repo.add(_source(), now=NOW)
    repo.record_success("blog", FetchState(etag='"old"'), now=NOW - timedelta(days=5))
    for _ in range(3):
        repo.record_failure("blog", "AdapterError: 0 entries", now=NOW)
    assert repo.get("blog").health == "broken"  # type: ignore[union-attr]
    new = RssRecipe(feed_url="https://blog.example.com/new-feed.xml")
    assert repo.replace_recipe("blog", new, discovery_run_id="discovery-1") is True
    record = repo.get("blog")
    assert record is not None and record.source.recipe == new
    assert record.health == "ok" and record.last_error is None  # it worked before; counts reset
    assert record.source.added_via == "discovery" and repo.fetch_state("blog") == FetchState()
    assert repo.discovery_run_id("blog") == "discovery-1"


def test_builtin_recipes_are_never_replaced(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    assert repo.replace_recipe("hf-blog", RssRecipe(feed_url=FEED), discovery_run_id=None) is False
    assert repo.get("hf-blog").source.recipe.type == "hf_blog"  # type: ignore[union-attr]
