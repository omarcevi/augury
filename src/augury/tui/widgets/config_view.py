import os
import platform
import re
import shlex
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import textual
from pydantic import BaseModel
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.theme import BUILTIN_THEMES
from textual.widgets import Static

from augury import __version__ as augury_version
from augury import schedule as scheduling
from augury.core.config import Config
from augury.core.db.connect import vec_version
from augury.core.db.sources_repo import SourcesRepo
from augury.core.paths import AppPaths
from augury.tui.safe_text import text
from augury.tui.ui_state import resolve_theme

if TYPE_CHECKING:
    from augury.tui.app import AuguryApp

SettingSource = Literal["default", "config.toml", "last used, ui_state.json", "running"]
EditorRunner = Callable[[list[str]], object]
MASKED_VALUE = "••••••"
# Config never holds a key (keys live in the environment or .env) -- this is a tripwire in
# case a future section ever grows one, so it can never be echoed on the config page/CLI.
_SECRET_FIELD = re.compile(r"(?:^|_)(?:api_key|key|secret|password|token)$")


def run_editor_subprocess(cmd: list[str]) -> None:
    """The real editor runner: an argv list, no shell. Tests inject their own instead, so
    a run never launches a real editor."""
    subprocess.run(cmd, check=False)


@dataclass(frozen=True)
class SettingRow:
    section: str
    field: str
    value: object
    source: SettingSource
    note: str = ""  # e.g. what config.toml says when something else wins


def _render_section_value(value: object) -> object:
    """A `PriceConfig`-shaped value (or any nested `BaseModel`) as one line, e.g.
    "input_per_mtok=1.5 output_per_mtok=6.0"."""
    if isinstance(value, BaseModel):
        return " ".join(f"{name}={getattr(value, name)}" for name in type(value).model_fields)
    return value


def effective_settings(config: BaseModel, raw_toml: Mapping[str, Any]) -> list[SettingRow]:
    """Every field of every section of `config`, generic over the pydantic model's own
    shape -- so a field added to any `_Section` (e.g. Lane 1/2's remember_state,
    reopen_last_article, copy_on_select, reading_width) shows up with no change here.
    Typed as `BaseModel`, not `Config`, on purpose: the whole point is that this walks
    whatever nested sections and fields the model actually declares, so it works
    unchanged on the real `Config` and on a stand-in shape a test builds to prove that.

    A section need not be a `_Section` itself: `Config.pricing` is a plain
    `dict[str, PriceConfig]` (there's no fixed set of fields to walk), so it gets one row
    per configured entry (keyed `"<spec>"`), or a single `*` / "none" placeholder row when
    it's empty.

    The source label is read straight from the raw TOML dict (`load_config`'s own
    parsing), not guessed: a field counts as "config.toml" only if its key is actually
    present in that section of the file the user wrote.
    """
    rows: list[SettingRow] = []
    for section_name in type(config).model_fields:
        section = getattr(config, section_name)
        raw_section = raw_toml.get(section_name)
        if not isinstance(raw_section, Mapping):
            raw_section = {}
        if not isinstance(section, BaseModel):
            if isinstance(section, Mapping):
                if not section:
                    rows.append(SettingRow(section_name, "*", "none", "default"))
                else:
                    for key, entry in section.items():
                        rows.append(
                            SettingRow(
                                section_name,
                                f'"{key}"',
                                _render_section_value(entry),
                                "config.toml",
                            )
                        )
            continue
        for field_name in type(section).model_fields:
            value = getattr(section, field_name)
            if _SECRET_FIELD.search(field_name) and value:
                value = MASKED_VALUE
            source: SettingSource = "config.toml" if field_name in raw_section else "default"
            rows.append(SettingRow(section_name, field_name, value, source))
    return rows


def theme_setting(
    config: Config,
    raw_toml: Mapping[str, Any],
    saved: str | None,
    themes: Collection[str],
    running: str | None = None,
) -> SettingRow:
    """tui.theme as the app runs it (P1): a theme picked with `t` (ui_state.json) wins over
    config.toml's, and one that isn't available falls back -- ui_state's own rule. `running` is
    the app's theme, when there is an app: config.toml may have changed since it launched."""
    theme, origin = resolve_theme(saved, config.tui.theme, themes)
    raw_tui = raw_toml.get("tui")
    in_file = isinstance(raw_tui, Mapping) and "theme" in raw_tui
    if running is not None and running != theme:  # it keeps its theme until the next launch
        if origin == "configured" and in_file:
            note = f"config.toml: {config.tui.theme} — applies on next launch"
        elif origin == "fallback" and in_file:
            note = f"config.toml: {config.tui.theme} isn't available — {theme} on next launch"
        else:
            note = f"{theme} on next launch"
        return SettingRow("tui", "theme", running, "running", note)
    source: SettingSource = "default"
    if origin == "saved":
        source = "last used, ui_state.json"
    elif origin == "configured" and in_file:
        source = "config.toml"
    note = f"config.toml: {config.tui.theme}" if in_file and config.tui.theme != theme else ""
    return SettingRow("tui", "theme", theme, source, note)


def ui_state_path(paths: AppPaths) -> Path:
    """Lane 1 owns `AppPaths.ui_state_file`; fall back to the same computed path until
    that lands here too."""
    return getattr(paths, "ui_state_file", paths.data_dir / "ui_state.json")


def editor_command(config_file: Path) -> list[str]:
    """$EDITOR, then $VISUAL, then a platform opener -- each split as a shell command
    line (so "code --wait" becomes its own argv), with the file appended."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        return [*shlex.split(editor), str(config_file)]
    opener = "open" if platform.system() == "Darwin" else "xdg-open"
    return [opener, str(config_file)]


@dataclass(frozen=True)
class ConfigReport:
    settings: list[SettingRow]
    paths: list[tuple[str, str]]
    sources_enabled: int
    sources_disabled: int
    sources_health: dict[str, int]
    schedule_message: str
    versions: list[tuple[str, str]]


def build_config_report(
    conn: sqlite3.Connection,
    config: Config,
    paths: AppPaths,
    raw_toml: Mapping[str, Any],
    *,
    saved_theme: str | None,
    themes: Collection[str] = BUILTIN_THEMES,
    running_theme: str | None = None,
) -> ConfigReport:
    """`saved_theme` is ui_state.json's (the app passes its own copy); `themes` the available
    ones (Textual's built-in ones, unless the app has more); `running_theme` the app's own."""
    records = SourcesRepo(conn).list_all()
    enabled = sum(1 for r in records if r.source.enabled)
    vec = vec_version(conn)
    theme = theme_setting(config, raw_toml, saved_theme, themes, running_theme)
    settings = [
        theme if (row.section, row.field) == ("tui", "theme") else row
        for row in effective_settings(config, raw_toml)
    ]
    return ConfigReport(
        settings=settings,
        paths=[
            ("config dir", str(paths.config_dir)),
            ("config.toml", str(paths.config_file)),
            ("interests.yaml", str(paths.interests_file)),
            ("data dir", str(paths.data_dir)),
            ("database", str(paths.db_file)),
            ("logs dir", str(paths.log_dir)),
            ("cache dir", str(paths.cache_dir)),
            ("export path", config.export.path or "off"),
            ("ui state", str(ui_state_path(paths))),
        ],
        sources_enabled=enabled,
        sources_disabled=len(records) - enabled,
        sources_health=dict(Counter(r.health for r in records)),
        schedule_message=scheduling.status().message,
        versions=[
            ("augury", augury_version),
            ("Python", platform.python_version()),
            ("Textual", textual.__version__),
            ("sqlite-vec", vec or "not loaded"),
            ("SQLite", sqlite3.sqlite_version),
        ],
    )


def render_config_text(report: ConfigReport) -> str:
    lines = ["Settings"]
    for row in report.settings:
        source = f"{row.source}; {row.note}" if row.note else row.source
        lines.append(f"  {row.section}.{row.field} = {row.value}  ({source})")
    lines += ["", "Paths"]
    lines += [f"  {label}: {value}" for label, value in report.paths]
    lines += ["", "Sources"]
    lines.append(f"  {report.sources_enabled} enabled, {report.sources_disabled} disabled")
    health = ", ".join(f"{n} {k}" for k, n in sorted(report.sources_health.items())) or "none yet"
    lines.append(f"  health: {health}")
    lines += ["", "Schedule"]
    lines.append(f"  {report.schedule_message}")
    lines += ["", "Versions"]
    lines += [f"  {label}: {value}" for label, value in report.versions]
    return "\n".join(lines)


class ConfigView(VerticalScroll):
    # "escape" shadows the app's own back_to_table binding, the same way SourcesView's
    # does -- that one only clears reading state, it never flips the ContentSwitcher back.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll_lines(1)", "scroll down", show=False),
        Binding("k", "scroll_lines(-1)", "scroll up", show=False),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text_content = ""

    def compose(self) -> ComposeResult:
        yield Static(id="config-text")

    def show(self, report: ConfigReport) -> None:
        self.text_content = render_config_text(report)
        self.query_one("#config-text", Static).update(text(self.text_content))

    def action_scroll_lines(self, lines: int) -> None:
        self.scroll_relative(y=lines, animate=False)

    def action_back(self) -> None:
        cast("AuguryApp", self.app).action_show_items()
