import sqlite3
from datetime import datetime

from augury.core.clock import utcnow
from augury.core.db.connect import connect
from augury.core.db.migrate import migrate
from augury.core.paths import AppPaths
from augury.sources.builtin import seed_builtin_sources


def open_db(paths: AppPaths, *, now: datetime | None = None) -> sqlite3.Connection:
    paths.ensure()
    conn = connect(paths.db_file)
    migrate(conn)
    seed_builtin_sources(conn, now=now or utcnow())
    return conn
