import re
import sqlite3
from importlib.resources import files

_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

Migration = tuple[int, str]


class SchemaTooNew(RuntimeError):
    pass


def available_migrations() -> list[Migration]:
    found: list[Migration] = []
    for entry in files("augury.core.db.migrations").iterdir():
        if match := _NAME.match(entry.name):
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> int:
    migrations = available_migrations() if migrations is None else sorted(migrations)
    numbers = [n for n, _ in migrations]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migration numbers must run 1..N with no gaps, got {numbers}")
    latest = numbers[-1] if numbers else 0
    current = schema_version(conn)
    if current > latest:
        raise SchemaTooNew(
            f"database schema v{current} is newer than this augury (knows v{latest}); "
            "upgrade augury"
        )
    for number, sql in migrations:
        if number <= current:
            continue
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)
