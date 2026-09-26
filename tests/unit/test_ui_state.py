import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from augury.tui import ui_state
from augury.tui.query import DIGEST_PRESET, ItemFilter
from augury.tui.ui_state import (
    FilterState,
    UiSession,
    UiState,
    effective_theme,
    load,
    resolve_theme,
    save,
)

NOW = datetime(2026, 9, 25, 9, 0, 30, 123456, tzinfo=UTC)
THEMES = ("textual-dark", "nord", "dracula")


def test_the_file_lives_in_the_data_dir(paths):
    assert paths.ui_state_file == paths.data_dir / "ui_state.json"


def test_a_missing_file_gives_the_defaults(paths):
    assert load(paths) == UiState()
    assert UiState().filter.to_filter(set()) == DIGEST_PRESET


def test_a_round_trip_keeps_everything(paths):
    state = UiState(
        theme="nord",
        last_visit_started_at=NOW - timedelta(days=1),
        current_visit_started_at=NOW,
        filter=FilterState.of(
            ItemFilter(search="qwen", sources=frozenset({"hf-blog"}), show="new")
        ),
        view="sources",
        selected_item_id="web:1",
        reading_item_id="web:2",
        reading_progress=0.4,
    )
    save(paths, state)
    assert load(paths) == state


@pytest.mark.parametrize("raw", [b"{not json", b"", b"[1, 2]", b"null", b'"text"', b"\xff\xfe{"])
def test_a_corrupt_file_gives_the_defaults_and_is_logged(paths, caplog, raw):
    paths.ui_state_file.write_bytes(raw)
    with caplog.at_level(logging.WARNING, logger=ui_state.__name__):
        assert load(paths) == UiState()
    assert "ui_state.json" in caplog.text


def test_an_unreadable_file_gives_the_defaults(paths, caplog):
    paths.ui_state_file.mkdir()  # reading a directory raises OSError
    with caplog.at_level(logging.WARNING, logger=ui_state.__name__):
        assert load(paths) == UiState()
    assert "ui_state.json" in caplog.text


def test_a_wrong_field_falls_back_alone_and_the_rest_survives(paths, caplog):
    paths.ui_state_file.write_text(
        json.dumps(
            {
                "theme": "nord",
                "current_visit_started_at": NOW.isoformat(),
                "filter": {"show": "bogus", "date": "7d"},
                "view": 3,
                "reading_progress": 7,
                "from_a_newer_version": True,
            }
        )
    )
    with caplog.at_level(logging.WARNING, logger=ui_state.__name__):
        state = load(paths)
    assert state.theme == "nord" and state.current_visit_started_at == NOW
    assert state.filter == FilterState() and state.view == "items"
    assert state.reading_progress is None
    assert "filter" in caplog.text


def test_a_naive_visit_time_is_rejected(paths):
    paths.ui_state_file.write_text(json.dumps({"current_visit_started_at": "2026-09-25T09:00:00"}))
    assert load(paths).current_visit_started_at is None


def files_in(directory) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def test_save_is_atomic_and_leaves_no_temp_files(paths):
    save(paths, UiState(theme="nord"))
    save(paths, UiState(theme="dracula"))
    assert load(paths).theme == "dracula"
    assert files_in(paths.data_dir) == ["ui_state.json"]


def test_a_failed_save_keeps_the_old_file_and_never_raises(paths, monkeypatch, caplog):
    save(paths, UiState(theme="nord"))

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with caplog.at_level(logging.WARNING, logger=ui_state.__name__):
        assert save(paths, UiState(theme="dracula")) is False
    assert load(paths).theme == "nord"
    assert files_in(paths.data_dir) == ["ui_state.json"]  # the temp file is cleaned up
    assert "disk full" in caplog.text


def test_save_creates_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUGURY_HOME", str(tmp_path / "fresh"))
    from augury.core.paths import app_paths

    fresh = app_paths()
    assert save(fresh, UiState(theme="nord")) is True
    assert load(fresh).theme == "nord"


def test_the_filter_drops_sources_and_kinds_that_no_longer_exist():
    state = FilterState(
        search="a\x1b[31mb",
        sources=["hf-blog", "removed-blog"],
        kinds=["paper", "podcast"],
        date="30d",
        sort="popular",
        show="new",
    )
    assert state.to_filter({"hf-blog", "hf-papers"}) == ItemFilter(
        search="ab",  # the search box shows it; no terminal escapes from a tampered file
        sources=frozenset({"hf-blog"}),
        kinds=frozenset({"paper"}),
        date="30d",
        sort="popular",
        show="new",
    )


def test_effective_theme_prefers_the_saved_one_then_the_config():
    assert effective_theme("nord", "dracula", THEMES) == "nord"
    assert effective_theme(None, "dracula", THEMES) == "dracula"
    assert effective_theme("gone-theme", "dracula", THEMES) == "dracula"
    assert effective_theme("gone-theme", "also-gone", THEMES) == "textual-dark"


def test_resolve_theme_also_says_which_one_won():
    # The config page (and `augury config`) shows where the running theme came from.
    assert resolve_theme("nord", "dracula", THEMES) == ("nord", "saved")
    assert resolve_theme(None, "dracula", THEMES) == ("dracula", "configured")
    assert resolve_theme("gone-theme", "dracula", THEMES) == ("dracula", "configured")
    assert resolve_theme("gone-theme", "also-gone", THEMES) == ("textual-dark", "fallback")


def test_a_first_visit_has_no_last_visit_and_is_saved_at_once(paths):
    session = UiSession.begin(paths, now=NOW)
    assert session.last_visit is None
    saved = load(paths)
    assert saved.current_visit_started_at == NOW.replace(microsecond=0)
    assert saved.last_visit_started_at is None


def test_the_last_visit_is_the_previous_sessions_start(paths):
    UiSession.begin(paths, now=NOW - timedelta(days=2))
    session = UiSession.begin(paths, now=NOW)
    assert session.last_visit == (NOW - timedelta(days=2)).replace(microsecond=0)
    saved = load(paths)
    assert saved.last_visit_started_at == session.last_visit
    assert saved.current_visit_started_at == NOW.replace(microsecond=0)


def test_beginning_a_visit_keeps_the_rest_of_the_state(paths):
    save(paths, UiState(theme="nord", view="sources", selected_item_id="web:1"))
    session = UiSession.begin(paths, now=NOW)
    assert session.previous.view == "sources" and session.previous.selected_item_id == "web:1"
    assert load(paths).theme == "nord" and load(paths).view == "sources"


def test_update_writes_only_when_something_changed(paths, monkeypatch):
    session = UiSession.begin(paths, now=NOW)
    writes: list[UiState] = []
    monkeypatch.setattr(ui_state, "save", lambda _paths, state: writes.append(state) or True)
    session.update(view="sources")
    session.update(view="sources")
    session.update(view="items")
    assert [w.view for w in writes] == ["sources", "items"]


def test_remember_theme_saves_at_once(paths):
    session = UiSession.begin(paths, now=NOW)
    session.remember_theme("dracula")
    assert load(paths).theme == "dracula"


def visits(paths, *, started, ended, last=None) -> None:
    save(
        paths,
        UiState(
            last_visit_started_at=last,
            current_visit_started_at=started,
            current_visit_ended_at=ended,
        ),
    )


BEFORE = (NOW - timedelta(days=2)).replace(microsecond=0)
PREVIOUS = (NOW - timedelta(minutes=30)).replace(microsecond=0)


def test_a_quick_look_does_not_count_as_a_visit(paths):
    visits(paths, started=PREVIOUS, ended=PREVIOUS + timedelta(seconds=20), last=BEFORE)
    session = UiSession.begin(paths, now=NOW)
    assert session.last_visit == BEFORE  # carried forward: the 20 s look didn't use up "new"
    assert load(paths).last_visit_started_at == BEFORE


def test_a_real_visit_moves_the_last_visit_on(paths):
    visits(paths, started=PREVIOUS, ended=PREVIOUS + timedelta(minutes=10), last=BEFORE)
    assert UiSession.begin(paths, now=NOW).last_visit == PREVIOUS


def test_a_visit_just_over_three_minutes_counts(paths):
    visits(paths, started=PREVIOUS, ended=PREVIOUS + timedelta(minutes=3), last=BEFORE)
    assert UiSession.begin(paths, now=NOW).last_visit == PREVIOUS


def test_a_crashed_visit_without_an_end_still_counts(paths):
    visits(paths, started=PREVIOUS, ended=None, last=BEFORE)
    assert UiSession.begin(paths, now=NOW).last_visit == PREVIOUS


def test_a_quick_first_visit_keeps_everything_new(paths):
    visits(paths, started=PREVIOUS, ended=PREVIOUS + timedelta(seconds=20), last=None)
    assert UiSession.begin(paths, now=NOW).last_visit is None


def test_beginning_clears_the_old_end_and_end_records_it(paths):
    visits(paths, started=PREVIOUS, ended=PREVIOUS + timedelta(minutes=10))
    session = UiSession.begin(paths, now=NOW)
    assert load(paths).current_visit_ended_at is None  # a crash now leaves no end behind
    session.end(now=NOW + timedelta(minutes=5))
    assert load(paths).current_visit_ended_at == (NOW + timedelta(minutes=5)).replace(microsecond=0)
