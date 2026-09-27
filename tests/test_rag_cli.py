import asyncio

from click.testing import CliRunner

from augury.cli import main
from augury.core.config import Config
from augury.core.db.open import open_db
from augury.core.db.runs_repo import RunsRepo
from augury.core.lock import ScoutLock
from augury.llm.embedder import EmbedMeter, HashEmbedder
from augury.rag.ingest import ingest_archive
from tests.rag.helpers import NOW, BrokenEmbedder, add_items


def test_reindex_without_a_key_rebuilds_the_keyword_index_only(paths):
    result = CliRunner().invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert "Keyword index rebuilt: 0 passages." in result.output
    assert "No vectors:" in result.output and "GEMINI_API_KEY" in result.output
    assert "keyword-only" in result.output


def test_reindex_waits_for_a_running_scout(paths):
    lock = ScoutLock(paths.scout_lock_file)
    lock.acquire()
    try:
        result = CliRunner().invoke(main, ["reindex"])
    finally:
        lock.release()
    assert result.exit_code != 0 and "when it has finished" in result.output


def _broken_embedder(monkeypatch) -> None:
    monkeypatch.setattr("augury.cli.rag.configured_embedder", lambda: (BrokenEmbedder(64), None))


def test_a_provider_error_stops_reindex_cleanly_and_fails_its_run(paths, monkeypatch):
    conn = open_db(paths, now=NOW)
    add_items(conn, [("Sparse attention", "long context"), ("Tomato soup", "a recipe")])
    asyncio.run(ingest_archive(conn, embedder=None, meter=None, now=lambda: NOW))
    conn.close()
    _broken_embedder(monkeypatch)
    result = CliRunner().invoke(main, ["reindex"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert "provider down" in result.output and "Traceback" not in result.output
    assert "run `augury reindex` again to carry on" in result.output
    run = RunsRepo(open_db(paths, now=NOW)).last("embed")
    assert run is not None and run.status == "failed"


def test_a_provider_error_stops_the_eval_cleanly_and_fails_its_run(paths, monkeypatch, tmp_path):
    from tests.rag.test_eval import seed_pairs

    conn = open_db(paths, now=NOW)
    seed_pairs(conn)
    meter = EmbedMeter(conn, Config(), RunsRepo(conn).start("scout", now=NOW), lambda: NOW)
    asyncio.run(ingest_archive(conn, embedder=HashEmbedder(64), meter=meter, now=lambda: NOW))
    conn.close()
    _broken_embedder(monkeypatch)  # the index's model, but the provider is down
    result = CliRunner().invoke(main, ["eval", "retrieval", "--out", str(tmp_path)])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert "provider down" in result.output and "Traceback" not in result.output
    run = RunsRepo(open_db(paths, now=NOW)).last("embed")
    assert run is not None and run.status == "failed"  # never left `running`
    assert list(tmp_path.glob("*.json")) == []
