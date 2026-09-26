"""What the TUI remembers between runs: the theme (P1), the last state (P2) and the last
visit (P11).

It lives in data/ui_state.json, never in config.toml: that's the user's own file and nothing
rewrites it. This file is only a convenience, so a missing, unreadable or corrupt one means
the defaults, and a partly wrong one loses only its wrong fields. The app always starts.
"""

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from augury.core.models import Kind
from augury.core.paths import AppPaths
from augury.core.text import strip_control_chars
from augury.tui.query import DIGEST_PRESET, DateRange, ItemFilter, ShowKey, SortKey

log = logging.getLogger(__name__)

FALLBACK_THEME = "textual-dark"
# A run shorter than this (quit, then relaunch) is a quick look, not a visit: it doesn't move
# "new since your last visit" on, or a relaunch 20 s later would show nothing new.
SHORT_VISIT = timedelta(minutes=3)
_KINDS = frozenset(get_args(Kind))


class FilterState(BaseModel):
    """An ItemFilter as JSON. The defaults are the Digest preset's."""

    search: str = DIGEST_PRESET.search
    sources: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    date: DateRange = DIGEST_PRESET.date
    sort: SortKey = DIGEST_PRESET.sort
    show: ShowKey = DIGEST_PRESET.show

    @classmethod
    def of(cls, f: ItemFilter) -> Self:
        return cls(
            search=f.search,
            sources=sorted(f.sources),
            kinds=sorted(f.kinds),
            date=f.date,
            sort=f.sort,
            show=f.show,
        )

    def to_filter(self, known_sources: Collection[str]) -> ItemFilter:
        """Sources (or kinds) that no longer exist are dropped silently."""
        return ItemFilter(
            search=strip_control_chars(self.search),
            sources=frozenset(s for s in self.sources if s in known_sources),
            kinds=frozenset(k for k in self.kinds if k in _KINDS),
            date=self.date,
            sort=self.sort,
            show=self.show,
        )


class UiState(BaseModel):
    # Only set by picking a theme in the app, so config.toml's theme applies until then.
    theme: str | None = None
    # The previous run's start (what "new since your last visit" compares with), and this one's.
    last_visit_started_at: AwareDatetime | None = None
    current_visit_started_at: AwareDatetime | None = None
    current_visit_ended_at: AwareDatetime | None = None  # set on quit; None after a crash
    filter: FilterState = Field(default_factory=FilterState)
    view: Literal["items", "sources"] = "items"
    selected_item_id: str | None = None
    # The article open in the reader at exit, and how far through it (a fraction).
    reading_item_id: str | None = None
    reading_progress: float | None = Field(default=None, ge=0, le=1)


def load(paths: AppPaths) -> UiState:
    path = paths.ui_state_file
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UiState()
    except (OSError, ValueError) as e:  # unreadable, not UTF-8, or not JSON
        log.warning("%s can't be used (%s); starting from the defaults", path, e)
        return UiState()
    if not isinstance(data, dict):
        log.warning("%s isn't a JSON object; starting from the defaults", path)
        return UiState()
    try:
        return UiState.model_validate(data)
    except ValidationError as e:
        wrong = {str(err["loc"][0]) for err in e.errors() if err["loc"]}
        log.warning("%s: ignoring %s (%s)", path, ", ".join(sorted(wrong)), e)
    try:  # keep the rest: a bad filter shouldn't also forget the theme and the last visit
        return UiState.model_validate({k: v for k, v in data.items() if k not in wrong})
    except ValidationError:
        return UiState()


def save(paths: AppPaths, state: UiState) -> bool:
    """Write atomically (a temp file in the same dir, then os.replace); never raises."""
    path = paths.ui_state_file
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ui_state.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(state.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        log.warning("couldn't save %s: %s", path, e)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False


ThemeOrigin = Literal["saved", "configured", "fallback"]


def resolve_theme(
    saved: str | None, configured: str, available: Collection[str]
) -> tuple[str, ThemeOrigin]:
    """P1: the theme last picked in the app, else config.toml's, else Textual's default; and
    which of those it is (the config page shows where the running theme came from)."""
    if saved and saved in available:
        return saved, "saved"
    if configured and configured in available:
        return configured, "configured"
    return FALLBACK_THEME, "fallback"


def effective_theme(saved: str | None, configured: str, available: Collection[str]) -> str:
    """P1: the theme last picked in the app, else config.toml's, else Textual's default."""
    return resolve_theme(saved, configured, available)[0]


class UiSession:
    """ui_state.json for one run of the TUI."""

    def __init__(self, paths: AppPaths, previous: UiState, state: UiState) -> None:
        self.paths = paths
        self.previous = previous  # as the last run left it: what P2 restores
        self.state = state  # as it is on disk now

    @classmethod
    def begin(cls, paths: AppPaths, *, now: datetime) -> Self:
        """P11: the previous run's start becomes this run's last visit, saved straight away
        (whatever `remember_state` says). Seconds only, like the items' first_seen. A quick
        look (a run that quit within SHORT_VISIT) keeps the last visit it had instead."""
        previous = load(paths)
        started, ended = previous.current_visit_started_at, previous.current_visit_ended_at
        quick = started is not None and ended is not None and ended - started < SHORT_VISIT
        state = previous.model_copy(
            update={
                "last_visit_started_at": previous.last_visit_started_at if quick else started,
                "current_visit_started_at": now.replace(microsecond=0),
                "current_visit_ended_at": None,
            }
        )
        save(paths, state)
        return cls(paths, previous, state)

    @property
    def last_visit(self) -> datetime | None:
        """None on a first launch: then everything is new."""
        return self.state.last_visit_started_at

    def update(self, **changes: Any) -> None:
        """Save the changes; nothing is written when nothing changed."""
        state = self.state.model_copy(update=changes)
        if state != self.state:
            self.state = state
            save(self.paths, state)

    def remember_theme(self, theme: str) -> None:
        self.update(theme=theme)

    def end(self, *, now: datetime) -> None:
        """On quit, so the next run can tell a quick look from a visit."""
        self.update(current_visit_ended_at=now.replace(microsecond=0))
