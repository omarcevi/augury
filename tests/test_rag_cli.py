from click.testing import CliRunner

from augury.cli import main
from augury.core.lock import ScoutLock


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
