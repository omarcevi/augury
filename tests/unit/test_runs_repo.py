from datetime import UTC, datetime, timedelta

from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo

NOW = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


def test_start_finish_and_last(paths):
    runs = RunsRepo(open_db(paths, now=NOW))
    run_id = runs.start("scout", now=NOW)
    runs.finish(
        run_id,
        "partial",
        now=NOW + timedelta(seconds=5),
        error="1 source failed",
        stats={"new_items": 3},
    )
    last = runs.last("scout")
    assert last is not None
    assert (last.id, last.status, last.stats["new_items"]) == (run_id, "partial", 3)
    assert runs.last("scout", statuses=("ok",)) is None


def test_running_rows_become_interrupted(paths):
    runs = RunsRepo(open_db(paths, now=NOW))
    runs.start("scout", now=NOW)
    assert runs.mark_running_as_interrupted("scout", now=NOW) == 1
    assert runs.last("scout").status == "interrupted"  # type: ignore[union-attr]
