import sqlite3

import pytest

from augury.core.clock import utcnow
from augury.core.db.connect import connect, vec_version
from augury.core.db.migrate import SchemaTooNew, migrate, schema_version
from augury.core.db.open import open_db


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_reaches_schema_v1(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert migrate(conn) == 1
    assert {
        "sources",
        "items",
        "items_fts",
        "signals",
        "contents",
        "item_state",
        "interactions",
        "runs",
    } <= _tables(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    assert migrate(conn) == 1


def test_newer_schema_is_refused(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNew, match="v99"):
        migrate(conn)


def test_failed_migration_rolls_back(tmp_path):
    conn = connect(tmp_path / "t.db")
    broken = [(1, "CREATE TABLE a (x INTEGER);\nCREATE TABLE a (x INTEGER);")]
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, broken)
    assert schema_version(conn) == 0
    assert "a" not in _tables(conn)


def test_migration_numbers_must_be_contiguous(tmp_path):
    conn = connect(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="no gaps"):
        migrate(conn, [(1, "SELECT 1;"), (3, "SELECT 1;")])


def test_fts_follows_items_through_triggers(tmp_path):
    conn = open_db_at(tmp_path)
    conn.execute(
        "INSERT INTO items (id, source_id, kind, title, url, canonical_url, first_seen, last_seen,"
        " content_hash) VALUES ('web:1', 'hf-blog', 'article', 'Qwen3-30B-A3B beats GRPO',"
        " 'https://x/1', 'https://x/1', '2026-09-25T00:00:00+00:00',"
        " '2026-09-25T00:00:00+00:00', 'h')"
    )
    hit = "SELECT count(*) FROM items_fts WHERE items_fts MATCH ?"
    assert conn.execute(hit, ('"Qwen3-30B-A3B"',)).fetchone()[0] == 1
    conn.execute("UPDATE items SET title = 'Something else' WHERE id = 'web:1'")
    assert conn.execute(hit, ('"Qwen3-30B-A3B"',)).fetchone()[0] == 0
    conn.execute("DELETE FROM items WHERE id = 'web:1'")
    assert conn.execute(hit, ('"Something"',)).fetchone()[0] == 0


def test_sqlite_vec_loads(tmp_path):
    assert vec_version(connect(tmp_path / "t.db")) is not None


def open_db_at(tmp_path) -> sqlite3.Connection:
    from augury.core.paths import AppPaths

    p = AppPaths(tmp_path / "c", tmp_path / "d", tmp_path / "k")
    return open_db(p, now=utcnow())
