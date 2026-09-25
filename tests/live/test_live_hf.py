import pytest

from augury.core.models import FetchState
from augury.sources.builtin import BUILTIN_SOURCES
from augury.sources.http import PoliteClient
from augury.sources.registry import adapter_for


@pytest.mark.live
@pytest.mark.parametrize("source", BUILTIN_SOURCES, ids=lambda s: s.id)
async def test_builtin_sources_fetch_live(source):
    async with PoliteClient() as http:
        result = await adapter_for(source).fetch(source, FetchState(), http)
    assert result.items, f"{source.id} returned nothing"
