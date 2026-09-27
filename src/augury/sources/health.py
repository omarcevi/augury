"""Source health for people (spec §4.4): what a state means, and where re-discovery starts.
The state itself is derived in core/db/sources_repo.health_state from what each fetch records."""

from urllib.parse import urlsplit

from augury.core.db.sources_repo import BROKEN_AFTER, RECIPE_URL_KEYS, SourceRecord


def health_note(record: SourceRecord) -> str | None:
    """One line on a degraded or broken source; None when it's fine or never fetched."""
    n = record.consecutive_failures
    last_ok = (
        f"last worked {record.last_success_at.astimezone():%Y-%m-%d %H:%M}"
        if record.last_success_at
        else "it has never worked"
    )
    if record.health == "broken":
        return f"broken: the last {n} fetches failed ({last_ok}). It stays enabled."
    if record.health == "degraded":
        more = BROKEN_AFTER - n
        return (
            f"degraded: the last {'fetch' if n == 1 else f'{n} fetches'} failed"
            f" ({last_ok}); {more} more and it counts as broken"
        )
    return None


def can_rediscover(record: SourceRecord) -> bool:
    return record.source.origin == "user"


def rediscover_target(record: SourceRecord) -> str:
    """Where discovery starts for a source that stopped working: its homepage, or else the
    site its recipe fetched from."""
    homepage = record.source.homepage
    if urlsplit(homepage).scheme in ("http", "https"):
        return homepage
    recipe = record.source.recipe.model_dump()
    for key in RECIPE_URL_KEYS:
        if isinstance(url := recipe.get(key), str):
            parts = urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}/"
    return record.source.name
