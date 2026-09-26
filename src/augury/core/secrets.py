"""API keys from <config>/.env (spec N5). Real environment variables always win, and values
are never logged or printed."""

import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from augury.core.paths import AppPaths

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER = "# API keys for augury, written by `augury init`. Keep this file private (mode 600).\n"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines. `#` comments, blank lines and an `export ` prefix are allowed, and
    matching quotes around a value are removed. Malformed lines are skipped and never echoed,
    because they may hold a secret."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if (match := _LINE.match(line)) is None:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(paths: AppPaths, environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Copy .env values into the environment where no real variable is set; returns the names."""
    try:
        values = read_env_file(paths.env_file)
    except OSError:  # unreadable: `augury doctor` points at the file; the app runs without it
        return []
    loaded = [key for key in values if key not in environ]
    for key in loaded:
        environ[key] = values[key]
    return loaded


def _format(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("a .env value can't contain a line break")
    if not any(ch.isspace() or ch in "#'\"" for ch in value):
        return value
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise ValueError("a .env value can't contain both kinds of quote")


def write_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Merge `updates` into .env, readable by the user only (mode 600), replaced atomically."""
    for key in updates:
        if not ENV_NAME.match(key):
            raise ValueError(f"not a valid variable name: {key!r}")
    values = read_env_file(path) | dict(updates)
    body = _HEADER + "".join(f"{key}={_format(value)}\n" for key, value in values.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(tmp, 0o600)  # O_CREAT's mode doesn't apply to a file that already existed
    os.replace(tmp, path)


def env_file_is_private(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
