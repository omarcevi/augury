import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from augury.core.paths import AppPaths


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoutConfig(_Section):
    auto_after_hours: float = Field(default=12.0, ge=0)
    enrich_max_per_run: int = Field(default=40, ge=0)
    prefetch_top_n: int = Field(default=10, ge=0)


class HttpConfig(_Section):
    min_interval_s: float = Field(default=1.0, ge=0)
    timeout_s: float = Field(default=20.0, gt=0)
    max_bytes: int = Field(default=10_000_000, gt=0)
    max_redirects: int = Field(default=5, ge=0)
    retries: int = Field(default=3, ge=0)


class ExportConfig(_Section):
    # A folder of portable Markdown (YAML frontmatter, relative links). Empty = no export.
    # Point it inside an Obsidian vault to read the digest there; nothing is Obsidian-specific.
    path: str = ""


class TuiConfig(_Section):
    theme: str = "textual-dark"
    # Restore the last filters, search, view and selected row on launch (data/ui_state.json).
    remember_state: bool = True
    # With remember_state, also reopen the article that was open at exit, where you left it.
    reopen_last_article: bool = False


class Config(_Section):
    scout: ScoutConfig = Field(default_factory=ScoutConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)


class Interests(_Section):
    audience: str = ""
    topics: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class ConfigError(Exception):
    """A config file exists but can't be used. The message says what to fix."""


def _describe(path: Path, err: ValidationError) -> str:
    lines = [f"{path} has {err.error_count()} problem(s):"]
    for e in err.errors():
        where = ".".join(str(part) for part in e["loc"]) or "(top level)"
        lines.append(f"  - {where}: {e['msg']}")
    return "\n".join(lines)


def load_config(paths: AppPaths) -> Config:
    path = paths.config_file
    if not path.exists():
        return Config()
    try:
        data: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_describe(path, e)) from e


def load_interests(paths: AppPaths) -> Interests:
    path = paths.interests_file
    if not path.exists():
        return Interests()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return Interests()
    try:
        return Interests.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_describe(path, e)) from e
