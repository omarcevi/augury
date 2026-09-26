import os
from datetime import UTC, datetime

import pytest

from augury.core.config import Config
from augury.llm.probes import probe_role
from augury.llm.resolver import default_resolver

pytestmark = pytest.mark.live
HAS_KEY = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


@pytest.mark.skipif(not HAS_KEY, reason="needs GEMINI_API_KEY")
async def test_the_default_fast_model_passes_the_doctor_probes():
    config = Config()
    result = await probe_role(
        "fast", config=config, resolver=default_resolver(config), now=lambda: datetime.now(UTC)
    )
    assert result.structured, result.detail
