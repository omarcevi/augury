import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec


def connect(db_file: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, autocommit=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if str(db_file) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _try_load_vec(conn)
    return conn


def _try_load_vec(conn: sqlite3.Connection) -> None:
    # Missing extension support is reported by `augury doctor`; nothing in M1 needs vectors.
    try:
        conn.enable_load_extension(True)
    except AttributeError:
        return
    try:
        sqlite_vec.load(conn)
    except sqlite3.OperationalError:
        pass
    finally:
        conn.enable_load_extension(False)


def vec_version(conn: sqlite3.Connection) -> str | None:
    try:
        return str(conn.execute("SELECT vec_version()").fetchone()[0])
    except sqlite3.OperationalError:
        return None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. Never await inside it: the connection is shared."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
