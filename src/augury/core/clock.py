from datetime import UTC, date, datetime, time, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone before storing it")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_api_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_day(dt: datetime) -> date:
    return dt.astimezone().date()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    # Local midnight to the next local midnight: 23 or 25 hours on DST-change days.
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return start, end
