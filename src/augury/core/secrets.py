"""API keys from <config>/.env (spec N5). Real environment variables always win, and values
are never logged or printed."""

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

from augury.core.paths import AppPaths

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


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
