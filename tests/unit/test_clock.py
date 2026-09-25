import time
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from augury.core.clock import from_iso, local_day_bounds, parse_api_datetime, to_iso


def test_iso_round_trip_is_utc():
    dt = datetime(2026, 9, 25, 9, 30, tzinfo=timezone(timedelta(hours=3)))
    s = to_iso(dt)
    assert s == "2026-09-25T06:30:00+00:00"
    assert from_iso(s) == dt


def test_to_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 9, 25))


def test_parse_api_datetime():
    assert parse_api_datetime("2026-09-21T00:00:00.000Z") == datetime(2026, 9, 21, tzinfo=UTC)
    assert parse_api_datetime("2026-09-21T00:00:00") == datetime(2026, 9, 21, tzinfo=UTC)
    assert parse_api_datetime("garbage") is None
    assert parse_api_datetime(None) is None


def test_local_day_bounds_run_midnight_to_midnight():
    start, end = local_day_bounds(date(2026, 9, 25))
    assert (start.hour, start.minute, end.hour, end.minute) == (0, 0, 0, 0)
    assert end.date() == date(2026, 9, 26) and start.tzinfo is not None


def test_local_day_bounds_follow_dst(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        start, end = local_day_bounds(date(2026, 11, 1))  # US clocks go back: a 25-hour day
        assert end - start == timedelta(hours=25)
    finally:
        monkeypatch.undo()
        time.tzset()
