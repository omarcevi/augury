import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from augury.core.config import ConfigError, load_config, load_interests
from augury.core.db.connect import vec_version
from augury.core.db.migrate import SchemaTooNew, schema_version
from augury.core.db.open import open_db
from augury.core.paths import AppPaths


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_paths(paths: AppPaths) -> list[Check]:
    return [
        Check("config dir", True, str(paths.config_dir)),
        Check("data dir", True, str(paths.data_dir)),
        Check("cache dir", True, str(paths.cache_dir)),
    ]


def check_config(paths: AppPaths) -> list[Check]:
    files: list[tuple[str, Callable[[AppPaths], object], Path]] = [
        ("config", load_config, paths.config_file),
        ("interests", load_interests, paths.interests_file),
    ]
    checks: list[Check] = []
    for name, loader, file in files:
        try:
            loader(paths)
        except ConfigError as e:
            checks.append(Check(name, False, str(e)))
            continue
        checks.append(Check(name, True, "ok" if file.exists() else "ok (defaults, no file yet)"))
    return checks


def check_database(paths: AppPaths) -> list[Check]:
    try:
        conn = open_db(paths)
    except SchemaTooNew as e:
        return [Check("database", False, str(e))]
    except sqlite3.Error as e:
        return [Check("database", False, f"{paths.db_file}: {e}")]
    try:
        version, vec = schema_version(conn), vec_version(conn)
    finally:
        conn.close()
    return [
        Check("database", True, f"{paths.db_file} (schema v{version})"),
        Check(
            "sqlite-vec",
            vec is not None,
            vec
            or "this Python can't load SQLite extensions (needed from M4); "
            "install with `uv tool install` to get a managed Python",
            required=False,
        ),
    ]


def run_checks(paths: AppPaths) -> list[Check]:
    return [*check_paths(paths), *check_config(paths), *check_database(paths)]


def format_checks(checks: list[Check]) -> str:
    def mark(c: Check) -> str:
        if c.ok:
            return "✓"
        return "✗" if c.required else "!"

    return "\n".join(f"{mark(c)} {c.name:<12} {c.detail}" for c in checks)


def failed(checks: list[Check]) -> bool:
    return any(not c.ok and c.required for c in checks)
