from augury.agents.scout import run_scout
from augury.core.db.sources_repo import SourcesRepo
from augury.sources.base import AdapterError
from tests.agents.test_scout import FakeAdapter, deps


async def test_a_breaking_source_is_degraded_after_one_scout_and_broken_after_three(paths):
    # Success criterion 4: a recipe that breaks shows up (⚠) within one scout.
    good = FakeAdapter(["A"])
    broken = FakeAdapter(error=AdapterError("the listing has 0 entries"))
    d = deps(paths, {"hf_papers": good, "hf_blog": good, "hf_community": good})
    repo = SourcesRepo(d.conn)
    await run_scout(d)
    assert repo.get("hf-blog").health == "ok"  # type: ignore[union-attr]
    d.adapters = {"hf_papers": good, "hf_blog": broken, "hf_community": good}
    await run_scout(d)
    assert repo.get("hf-blog").health == "degraded"  # type: ignore[union-attr]
    await run_scout(d)
    await run_scout(d)
    record = repo.get("hf-blog")
    assert record is not None and record.health == "broken" and record.source.enabled
    assert "0 entries" in (record.last_error or "")
    d.adapters = {"hf_papers": good, "hf_blog": good, "hf_community": good}
    await run_scout(d)
    assert repo.get("hf-blog").health == "ok"  # type: ignore[union-attr]
