import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from augury.core.paths import AppPaths


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


AGENTS = ("triage", "summarizer", "discovery", "ask")


def _packaged_default_models() -> dict[str, str]:
    text = files("augury.llm").joinpath("defaults.toml").read_text(encoding="utf-8")
    return dict(tomllib.loads(text)["models"])


DEFAULT_MODELS = _packaged_default_models()


def _model_spec(value: str) -> str:
    provider, sep, name = value.partition("/")
    if not (provider and sep and name):
        raise ValueError(f"{value!r} must look like provider/model, e.g. gemini/<model-id>")
    return value


ModelSpec = Annotated[str, AfterValidator(_model_spec)]


class ModelsConfig(_Section):
    fast: ModelSpec = DEFAULT_MODELS["fast"]
    smart: ModelSpec = DEFAULT_MODELS["smart"]
    overrides: dict[str, ModelSpec] = Field(default_factory=dict)  # per-agent pins

    @field_validator("overrides")
    @classmethod
    def _known_agents(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(AGENTS))
        if unknown:
            raise ValueError(f"unknown agent(s): {', '.join(unknown)} (known: {', '.join(AGENTS)})")
        return value


class GoogleConfig(_Section):
    # Vertex AI only. API keys never go in config.toml (spec N5); they live in .env.
    project: str = ""
    location: str = ""


class ScoutConfig(_Section):
    # 0 disables auto-scout. At launch, a scout also runs if the last one was on an earlier
    # local day (so a stale morning launch doesn't show yesterday's empty digest) -- but
    # only while this stays <= 24h; past that the user wants a longer, deliberate interval
    # (e.g. every 2 days), and midnight must not override it.
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


class BudgetConfig(_Section):
    daily_usd: float = Field(default=1.00, ge=0)  # 0 turns every LLM feature off
    daily_tokens: int = Field(default=2_000_000, ge=0)  # for models without a known price


class PriceConfig(_Section):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class RankingConfig(_Section):
    # Spec §5.4 defaults. Only the ratios matter: weights are renormalized for each item.
    w_rel: float = Field(default=0.50, ge=0)
    w_pop: float = Field(default=0.25, ge=0)
    w_rec: float = Field(default=0.15, ge=0)
    w_nov: float = Field(default=0.10, ge=0)
    # No model key (user decision 2026-09-26): your ★ likes plus recency. Until you like
    # something these are missing, and the digest ranks on w_pop and w_rec above.
    w_topic: float = Field(default=0.45, ge=0)  # titles and summaries like the ones you liked
    w_source: float = Field(default=0.20, ge=0)  # sources you like often
    w_fresh: float = Field(default=0.35, ge=0)  # recency, in this mode
    like_terms: int = Field(default=30, ge=1, le=200)  # top terms taken from liked items

    @model_validator(mode="after")
    def _some_weight(self) -> Self:
        if self.w_rel + self.w_pop + self.w_rec + self.w_nov <= 0:
            raise ValueError("at least one [ranking] weight must be above 0")
        if self.w_topic + self.w_source + self.w_fresh <= 0:
            raise ValueError("at least one of w_topic, w_source, w_fresh must be above 0")
        return self


class SummarizerConfig(_Section):
    max_input_chars: int = Field(default=120_000, ge=1_000)  # spec §5.5


class TuiConfig(_Section):
    theme: str = "textual-dark"
    # Restore the last filters, search, view and selected row on launch (data/ui_state.json).
    remember_state: bool = True
    # With remember_state, also reopen the article that was open at exit, where you left it.
    reopen_last_article: bool = False
    # Finishing a mouse selection copies it (OSC 52, plus pbcopy/wl-copy/xclip when local).
    copy_on_select: bool = True
    # The reader's text column in cells (zen mode centres it; the rest becomes margins).
    reading_width: int = Field(default=88, ge=40, le=200)
    # How the reader's TL;DR box starts: shown, or collapsed to one line (h toggles).
    # Collapsed writes no TL;DRs until you expand it.
    tldr: Literal["shown", "collapsed"] = "shown"


class Config(_Section):
    scout: ScoutConfig = Field(default_factory=ScoutConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    pricing: dict[str, PriceConfig] = Field(default_factory=dict)  # [pricing."<provider/model>"]


class Interests(_Section):
    about: str = ""
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


def load_raw_toml(paths: AppPaths) -> dict[str, Any]:
    """config.toml's own keys, verbatim -- used only to tell the config page (P3) which
    effective settings came from the file vs. a pydantic default. Never used to validate;
    `load_config` above is the only source of truth for that, and any error here (a
    missing or unreadable file) just means "nothing came from the file"."""
    path = paths.config_file
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}


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
        hint = "Run `augury init` to recreate it (it asks before overwriting)."
        raise ConfigError(f"{_describe(path, e)}\n{hint}") from e
