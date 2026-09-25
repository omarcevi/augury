from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Protocol

from augury.core.models import FetchResult, FetchState, RawItem, Source
from augury.sources.http import HttpClient


class AdapterError(Exception):
    """The source answered, but not in a shape we can use."""


class Adapter(Protocol):
    recipe_type: ClassVar[str]

    async def fetch(self, source: Source, state: FetchState, http: HttpClient) -> FetchResult: ...


Mapper = Callable[[str, dict[str, Any], int], RawItem]


def map_entries(
    source_id: str, entries: Sequence[Any], mapper: Mapper
) -> tuple[list[RawItem], int]:
    """Map entries one by one; a malformed entry is skipped, an empty listing is an error."""
    if not entries:
        raise AdapterError("the listing has 0 entries (the source or its API may have changed)")
    items: list[RawItem] = []
    skipped = 0
    for rank, entry in enumerate(entries, start=1):
        try:
            items.append(mapper(source_id, entry, rank))
        except KeyError, TypeError, ValueError:  # pydantic's ValidationError is a ValueError
            skipped += 1
    if not items:
        raise AdapterError(f"none of the {len(entries)} entries had the expected fields")
    return items, skipped
