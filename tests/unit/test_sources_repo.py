from datetime import UTC, datetime

import pytest

from augury.core.db.open import open_db
from augury.core.db.sources_repo import BuiltinSourceError, SourceExists, SourcesRepo
from augury.core.models import FetchState, RssRecipe, Source

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


def _user_source(sid: str = "example-blog") -> Source:
    return Source(
        id=sid,
        name="Example",
        origin="user",
        recipe=RssRecipe(feed_url="https://example.com/feed.xml"),
    )


def test_builtins_are_seeded_once_and_disabling_sticks(paths):
    conn = open_db(paths, now=NOW)
    repo = SourcesRepo(conn)
    assert [r.source.id for r in repo.list_all()] == ["hf-blog", "hf-community", "hf-papers"]
    repo.set_enabled("hf-community", False)
    conn.close()
    repo = SourcesRepo(open_db(paths, now=NOW))
    assert [r.source.id for r in repo.list_all(enabled_only=True)] == ["hf-blog", "hf-papers"]


def test_add_get_and_duplicate(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    repo.add(_user_source(), now=NOW)
    record = repo.get("example-blog")
    assert record is not None and record.source.recipe == RssRecipe(
        feed_url="https://example.com/feed.xml"
    )
    assert record.health == "never"
    with pytest.raises(SourceExists):
        repo.add(_user_source(), now=NOW)
    assert repo.find_by_feed_url("https://example.com/feed.xml") == "example-blog"
    assert repo.unique_id("example-blog") == "example-blog-2"


def test_a_suffixed_id_still_fits_the_id_pattern(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    base = "a" * 60 + "-bc"  # 63 characters, the longest id Source allows
    repo.add(_user_source(base), now=NOW)
    assert repo.unique_id(base) == "a" * 60 + "-2"  # cut to make room, with no double dash
    repo.add(_user_source("a" * 60 + "-2"), now=NOW)
    assert repo.unique_id(base) == "a" * 60 + "-3"
    Source.model_validate(_user_source().model_dump() | {"id": repo.unique_id(base)})


def test_health_follows_success_and_failures(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    repo.record_success("hf-papers", FetchState(etag='"abc"'), now=NOW)
    assert repo.get("hf-papers").health == "ok"  # type: ignore[union-attr]
    assert repo.fetch_state("hf-papers").etag == '"abc"'
    for expected in ("degraded", "degraded", "broken"):
        repo.record_failure("hf-papers", "HttpError: HTTP 503", now=NOW)
        assert repo.get("hf-papers").health == expected  # type: ignore[union-attr]
    repo.record_success("hf-papers", FetchState(), now=NOW)
    assert repo.get("hf-papers").consecutive_failures == 0  # type: ignore[union-attr]


def test_builtins_cannot_be_removed_but_user_sources_can(paths):
    repo = SourcesRepo(open_db(paths, now=NOW))
    with pytest.raises(BuiltinSourceError):
        repo.remove("hf-papers")
    repo.add(_user_source(), now=NOW)
    assert repo.remove("example-blog") is True
    assert repo.remove("example-blog") is False
