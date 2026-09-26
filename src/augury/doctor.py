import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from augury.core.clock import utcnow
from augury.core.config import Config, ConfigError, load_config, load_interests
from augury.core.db.connect import vec_version
from augury.core.db.migrate import SchemaTooNew, schema_version
from augury.core.db.open import open_db
from augury.core.paths import AppPaths
from augury.core.secrets import env_file_is_private
from augury.llm.budget import budget_problem, today_spend
from augury.llm.pricing import price_for
from augury.llm.probes import ProbeResult, probe_role, save_probe_results
from augury.llm.resolver import Resolver, role_statuses
from augury.sources.http import HttpClient, HttpError, NetworkError


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


async def check_network(http: HttpClient) -> list[Check]:
    url = "https://huggingface.co/api/daily_papers?limit=1"
    try:
        await http.get(url)
    except NetworkError as e:
        return [Check("network", False, f"network unreachable (offline?): {e}")]
    except HttpError as e:
        return [Check("network", False, f"can't reach Hugging Face: {e}")]
    return [Check("network", True, "huggingface.co reachable")]


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


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def _todays_budget_problem(
    paths: AppPaths, config: Config, now: Callable[[], datetime]
) -> str | None:
    try:
        conn = open_db(paths)
    except sqlite3.Error, SchemaTooNew:
        return None  # the database check already explains it
    try:
        return budget_problem(today_spend(conn, now()), config.budget)
    finally:
        conn.close()


def check_ai(
    paths: AppPaths,
    config: Config,
    resolver: Resolver,
    probes: Mapping[str, ProbeResult] | None = None,
    *,
    now: Callable[[], datetime] = utcnow,
) -> list[Check]:
    """Model auth, probe results, prices and the budget. Never `required`: AI is optional."""
    checks: list[Check] = []
    unpriced: set[str] = set()
    for status in role_statuses(config, resolver):
        if not status.ok:
            checks.append(Check(status.role, False, status.detail, required=False))
            continue
        probe = (probes or {}).get(status.role)
        if probe is None:
            detail = f"{status.spec}: key found (run `augury doctor` without --offline to probe it)"
            checks.append(Check(status.role, True, detail, required=False))
        else:
            detail = (
                f"{status.spec} · structured output {_mark(probe.structured)}"
                f" · tool call {_mark(probe.tools)}"
            )
            if probe.detail:
                detail += f" ({probe.detail})"
            checks.append(Check(status.role, probe.structured, detail, required=False))
        priced = price_for(status.spec, config, native=status.native) is not None
        if status.spec not in unpriced and not priced:
            unpriced.add(status.spec)
            cap = f"{config.budget.daily_tokens:,}"
            fix = f'add [pricing."{status.spec}"] to config.toml'
            detail = f"{status.spec}: price unknown, so the {cap}-token daily cap applies ({fix})"
            checks.append(Check("pricing", False, detail, required=False))
    if (problem := _todays_budget_problem(paths, config, now)) is not None:
        detail = f"{problem}; AI pauses until tomorrow"
        checks.append(Check("budget", False, detail, required=False))
    if paths.env_file.exists() and not env_file_is_private(paths.env_file):
        checks.append(
            Check(
                ".env",
                False,
                f"{paths.env_file} is readable by other users: run chmod 600 {paths.env_file}",
                required=False,
            )
        )
    return checks


async def probe_models(
    paths: AppPaths, config: Config, resolver: Resolver, *, now: Callable[[], datetime] = utcnow
) -> dict[str, ProbeResult]:
    """Probe every configured role (unless today's budget is spent) and save the results."""
    if _todays_budget_problem(paths, config, now) is not None:
        return {}
    results: dict[str, ProbeResult] = {}
    for status in role_statuses(config, resolver):
        if status.ok:
            results[status.role] = await probe_role(
                status.role, config=config, resolver=resolver, now=now
            )
    if results:
        save_probe_results(paths.probe_cache_file, results.values())
    return results
