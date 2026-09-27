import os
import platform
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
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.theme import BUILTIN_THEMES
from textual.widget import Widget
from textual.widgets import DataTable, Static

from augury import __version__ as augury_version
from augury import schedule as scheduling
from augury.core.config import Config, ConfigError, Interests
from augury.core.config_edit import (
    INTEREST_FIELDS,
    SECRET_FIELD,
    bounds,
    check_interest,
    check_value,
    choices,
    default_of,
    editor_kind,
    ensure_editable,
    ensure_interests_editable,
    reset_interest,
    reset_value,
    set_interest,
    set_value,
)
from augury.core.db.connect import vec_version
from augury.core.db.sources_repo import SourcesRepo
from augury.core.paths import AppPaths
from augury.tui.safe_text import text
from augury.tui.ui_state import resolve_theme
from augury.tui.widgets.input_modal import InputModal
from augury.tui.widgets.picker_modal import ChoiceModal

if TYPE_CHECKING:
    from augury.tui.app import AuguryApp

SettingSource = Literal[
    "default", "config.toml", "interests.yaml", "last used, ui_state.json", "running"
]
EditorRunner = Callable[[list[str]], object]
MASKED_VALUE = "••••••"


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
            if SECRET_FIELD.search(field_name) and value:
                value = MASKED_VALUE
            source: SettingSource = "config.toml" if field_name in raw_section else "default"
            rows.append(SettingRow(section_name, field_name, value, source))
    return rows


def interest_settings(interests: Interests, raw: Mapping[str, Any]) -> list[SettingRow]:
    """P15: interests.yaml's about, topics and avoid (lists comma-joined). The source is
    "interests.yaml" only for a key the file has and that isn't empty there."""
    rows: list[SettingRow] = []
    for name in INTEREST_FIELDS:
        value = getattr(interests, name)
        shown = ", ".join(value) if isinstance(value, list) else value
        source: SettingSource = "interests.yaml" if raw.get(name) else "default"
        rows.append(SettingRow("interests", name, shown, source))
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
    interests: Interests | None = None,
    raw_interests: Mapping[str, Any] | None = None,
) -> ConfigReport:
    """`saved_theme` is ui_state.json's (the app passes its own copy); `themes` the available
    ones (Textual's built-in ones, unless the app has more); `running_theme` the app's own.
    `interests` adds interests.yaml's rows after config.toml's (none when it can't be loaded);
    `raw_interests` is the file's own keys, for their source."""
    records = SourcesRepo(conn).list_all()
    enabled = sum(1 for r in records if r.source.enabled)
    vec = vec_version(conn)
    theme = theme_setting(config, raw_toml, saved_theme, themes, running_theme)
    settings = [
        theme if (row.section, row.field) == ("tui", "theme") else row
        for row in effective_settings(config, raw_toml)
    ]
    if interests is not None:
        settings += interest_settings(interests, raw_interests or {})
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


def source_label(row: SettingRow) -> str:
    return f"{row.source}; {row.note}" if row.note else row.source


def _settings_lines(report: ConfigReport) -> list[str]:
    lines = ["Settings"]
    for row in report.settings:
        lines.append(f"  {row.section}.{row.field} = {row.value}  ({source_label(row)})")
    return lines


def render_details_text(report: ConfigReport) -> str:
    """Everything but the settings: the config page shows those as a table (P14)."""
    lines = ["Paths"]
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


def render_config_text(report: ConfigReport) -> str:
    """The whole report as text: `augury config` prints it, `y` on the config page copies it."""
    return "\n".join([*_settings_lines(report), "", render_details_text(report)])


Applies = Literal["now", "scout", "launch"]
APPLIES_TEXT: dict[Applies, str] = {
    "now": "applies now",
    "scout": "applies from the next scout",
    "launch": "applies on next launch",
}
# What the running app reads again once a save replaces app.config (AuguryApp.adopt_config), by
# "section.field" or a whole section. Everything else is read once, at launch: the resolver and
# the budget ([models], [google], [budget]), the reader's Summarizer ([summarizer]), the launch's
# own auto-scout and restore (auto_after_hours, remember_state, reopen_last_article).
_APPLIES: dict[str, Applies] = {
    "tui.theme": "now",  # applied at once
    "tui.reading_width": "now",  # the reader's text column, at once
    "tui.copy_on_select": "now",  # read on every selection
    "tui.tldr": "now",  # the reader's TL;DR box, at once (the scout's prefetch reads it too)
    "scout.enrich_max_per_run": "scout",  # the scout reads app.config when it starts
    "scout.prefetch_top_n": "scout",
    "http": "scout",  # the next scout builds a new client from it
    "export": "scout",
    "ranking": "scout",
    "interests": "scout",  # the next scout's triage reads app.interests
    "search": "now",  # each discovery run reads app.config
}


def applies(section: str, field: str) -> Applies:
    return _APPLIES.get(f"{section}.{field}") or _APPLIES.get(section) or "launch"


def _shown(value: object) -> str:
    return "(blank)" if value == "" else str(value)


INTEREST_HINTS = {  # the examples `augury init` gives
    "about": "What you do, in one line, so the AI can judge what's useful to you, "
    "e.g. ML engineer building RAG apps",
    "topics": "comma-separated, e.g. LLM agents, RAG, diffusion models, robotics",
    "avoid": "comma-separated, e.g. crypto, AI art, funding rounds",
}


def input_hint(section: str, field: str) -> str:
    """The line above an input: what's allowed, and the default."""
    if (section, field) == ("export", "path"):
        return "A folder for the Markdown digests (~ is your home). Blank turns the export off."
    default = default_of(section, field)
    parts: list[str] = []
    if section == "models":
        parts.append("provider/model, e.g. gemini/<model-id>")
    elif rule := bounds(section, field):
        parts.append(rule)
    parts.append(f"default {default}" if default != "" else "default: blank")
    return " · ".join(parts)


class SettingsTable(DataTable[Text]):
    """Every setting, one row each (P14). Enter or space edits the selected one, backspace or
    delete resets it to the default. The keys are the app's config-only actions."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "app.edit_setting", "edit", show=False),  # instead of select_cursor
        Binding("space", "app.edit_setting", "edit", show=False),
        Binding("backspace", "app.reset_setting", "reset", show=False),
        Binding("delete", "app.reset_setting", "reset", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("g", "scroll_top", "top", show=False),
        Binding("G", "scroll_bottom", "bottom", show=False),
    ]
    COLUMNS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("setting", "Setting"),
        ("value", "Value"),
        ("source", "Source"),
    )

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(cursor_type="row", id=id)
        self.settings: list[SettingRow] = []
        self._widths: tuple[int | None, ...] = ()

    def show(self, settings: list[SettingRow]) -> None:
        """Redraw with these rows; the cursor stays where it was."""
        self.settings = list(settings)
        self._draw(self.cursor_row)

    def current(self) -> SettingRow | None:
        if not self.settings or self.cursor_row < 0:
            return None
        return self.settings[min(self.cursor_row, len(self.settings) - 1)]

    def _cells(self, row: SettingRow) -> tuple[str, str, str]:
        return f"{row.section}.{row.field}", str(row.value), source_label(row)

    def _measure(self) -> tuple[int | None, ...]:
        """Natural widths when they fit; else the names stay whole, and the value and source
        share the rest (a longer one ends in …: enter shows a value in full, y copies them)."""
        cells = [self._cells(row) for row in self.settings]
        natural = [
            max([cell_len(label), *(cell_len(c[i]) for c in cells)])
            for i, (_, label) in enumerate(self.COLUMNS)
        ]
        free = self.size.width - 2 * self.cell_padding * len(self.COLUMNS) - 1
        if not self.size.width or sum(natural) <= free:
            return (None, None, None)
        rest = max(24, free - natural[0])
        value = min(natural[1], max(12, rest - min(natural[2], 24)))
        return natural[0], value, max(8, rest - value)

    def _draw(self, cursor: int) -> None:
        self._widths = self._measure()
        self.clear(columns=True)
        for (key, label), width in zip(self.COLUMNS, self._widths, strict=True):
            self.add_column(label, key=key, width=width)
        for row in self.settings:
            name, value, source = self._cells(row)
            style = "dim" if row.source == "default" else ""
            self.add_row(
                text(name, one_line=True),
                text(value, one_line=True),  # literally: a value is never markup
                text(source, style, one_line=True),
                key=name,
            )
        if self.settings:
            self.move_cursor(row=min(max(cursor, 0), len(self.settings) - 1))

    def on_resize(self, _event: events.Resize) -> None:
        if self.settings and self._measure() != self._widths:
            self._draw(self.cursor_row)

    # The table is as tall as its rows and the page around it scrolls (ConfigView), so moving
    # by a page means by what the page shows, and down from the last row reads on below it.
    def _page(self) -> int:
        view = self.parent
        return max(1, view.scrollable_content_region.height - 2) if isinstance(view, Widget) else 1

    def action_cursor_down(self) -> None:
        view = self.parent
        if self.row_count and self.cursor_row >= self.row_count - 1 and isinstance(view, Widget):
            view.scroll_relative(y=1, animate=False)
            return
        super().action_cursor_down()

    def action_page_down(self) -> None:
        self.move_cursor(row=min(self.cursor_row + self._page(), self.row_count - 1))

    def action_page_up(self) -> None:
        self.move_cursor(row=max(self.cursor_row - self._page(), 0))


class ConfigView(VerticalScroll):
    """The config page (view 3): the settings table, then the paths, sources, schedule and
    versions, in one scroll that follows the table's cursor."""

    # "escape" shadows the app's own back_to_table binding, the same way SourcesView's
    # does -- that one only clears reading state, it never flips the ContentSwitcher back.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll_lines(1)", "scroll down", show=False),
        Binding("k", "scroll_lines(-1)", "scroll up", show=False),
        Binding("y", "app.copy_config", "copy", show=False),
        Binding("Y", "app.copy_config", "copy", show=False),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text_content = ""  # the whole report, as `augury config` prints it

    def compose(self) -> ComposeResult:
        yield Static(text("Settings", "bold"), id="config-title")
        yield SettingsTable(id="settings")
        yield Static(id="config-text")

    @property
    def table(self) -> SettingsTable:
        return self.query_one(SettingsTable)

    @property
    def _app(self) -> AuguryApp:
        return cast("AuguryApp", self.app)

    def show(self, report: ConfigReport) -> None:
        self.text_content = render_config_text(report)
        self.table.show(report.settings)
        self.query_one("#config-text", Static).update(text(render_details_text(report)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.table:
            self.call_after_refresh(self._follow_cursor)

    def _follow_cursor(self) -> None:
        """The table is as tall as its rows, so this scroll (not the table's) keeps the cursor
        in view: the first row shows the title too, the last one as much of the text below it
        as fits (j goes on from there)."""
        table = self.table
        header = table.header_height if table.show_header else 0
        y = table.virtual_region.y + header + table.cursor_row  # every row is one line
        if table.cursor_row <= 0:
            self.scroll_home(animate=False, immediate=True)
        elif table.cursor_row >= table.row_count - 1:
            self.scroll_to(y=min(y, self.max_scroll_y), animate=False, immediate=True, force=True)
        else:
            self.scroll_to_region(Region(0, y, 1, 1), animate=False, immediate=True, force=True)

    def action_scroll_lines(self, lines: int) -> None:
        self.scroll_relative(y=lines, animate=False)

    def action_back(self) -> None:
        self._app.action_show_items()

    # -- editing (P14): the app's edit_setting / reset_setting actions land here --------------

    def _editable(self, row: SettingRow) -> bool:
        """Says why not, in a toast, when the selected row can't be changed here."""
        app, name = self._app, f"{row.section}.{row.field}"
        if row.section == "interests":
            try:
                ensure_interests_editable(app.paths)
            except ConfigError as e:
                app.notify(str(e), severity="error", markup=False)
                return False
            return True
        kind = editor_kind(row.section, row.field)
        if kind == "secret" or row.value == MASKED_VALUE:
            app.notify(f"{name} looks like a secret: it's never shown or edited here", markup=False)
            return False
        if kind == "file":
            app.notify("Edit this one in config.toml (press e)", markup=False)
            return False
        try:
            ensure_editable(app.paths)
        except ConfigError as e:
            app.notify(str(e), severity="error", markup=False)
            return False
        return True

    def edit_selected(self) -> None:
        """An editor chosen by the field's type: a bool flips, a Literal (and the theme) is
        picked from its values, a number or a string is typed in and checked before saving."""
        row = self.table.current()
        if row is None or not self._editable(row):
            return
        app, section, field = self._app, row.section, row.field
        name = f"{section}.{field}"
        if section == "interests":
            self._edit_interest(field)
            return
        current = getattr(getattr(app.config, section), field)

        def save(value: str | None) -> None:
            if value is not None:
                self.save(section, field, value)

        def problem(value: str) -> str | None:
            try:
                check_value(app.paths, section, field, value)
            except ConfigError as e:
                return str(e)
            return None

        kind = editor_kind(section, field)
        if (section, field) == ("tui", "theme"):
            themes = [(theme, theme) for theme in sorted(app.available_themes)]
            app.push_screen(ChoiceModal(name, themes, app.theme), save)
        elif kind == "bool":
            self.save(section, field, not current)
        elif kind == "choice":
            options = [(value, value) for value in choices(section, field)]
            app.push_screen(ChoiceModal(name, options, str(current)), save)
        else:
            hint = input_hint(section, field)
            app.push_screen(InputModal(name, str(current), hint, problem), save)

    def _edit_interest(self, field: str) -> None:
        """P15: about is a line of text, topics and avoid a comma-separated list."""
        app = self._app
        current = getattr(app.interests, field)
        value = ", ".join(current) if isinstance(current, list) else current

        def save(typed: str | None) -> None:
            if typed is not None:
                self.save_interest(field, typed)

        def problem(typed: str) -> str | None:
            try:
                check_interest(app.paths, field, typed)
            except ConfigError as e:
                return str(e)
            return None

        modal = InputModal(f"interests.{field}", value, INTEREST_HINTS[field], problem)
        app.push_screen(modal, save)

    def save_interest(self, field: str, value: object) -> None:
        app = self._app
        try:
            interests = set_interest(app.paths, field, value)  # validated before it's written
        except ConfigError as e:
            app.notify(str(e), severity="error", markup=False)
            return
        app.adopt_interests(interests)
        app.notify(f"Saved interests.{field} · {APPLIES_TEXT['scout']}", markup=False)

    def save(self, section: str, field: str, value: object) -> None:
        app = self._app
        try:
            config = set_value(app.paths, section, field, value)  # validated before it's written
        except ConfigError as e:
            app.notify(str(e), severity="error", markup=False)
            return
        app.adopt_config(config, section, field)
        saved = _shown(getattr(getattr(config, section), field))
        when = APPLIES_TEXT[applies(section, field)]
        app.notify(f"Saved {section}.{field} = {saved} · {when}", markup=False)

    def reset_selected(self) -> None:
        """Back to the built-in default: the key leaves config.toml (and its section, if that
        empties it). A theme picked with t is an override too, so a theme reset drops that."""
        row = self.table.current()
        if row is None or not self._editable(row):
            return
        app, section, field = self._app, row.section, row.field
        name = f"{section}.{field}"
        if section == "interests":
            self._clear_interest(field)
            return
        try:
            config = reset_value(app.paths, section, field)
        except ConfigError as e:
            app.notify(str(e), severity="error", markup=False)
            return
        picked = app.session.state.theme if app.session is not None else None  # with t
        if config is None and not ((section, field) == ("tui", "theme") and picked):
            app.notify(f"{name} is already the default", markup=False, timeout=2)
            return
        config = config or app.config
        app.adopt_config(config, section, field)
        value = _shown(getattr(getattr(config, section), field))
        when = APPLIES_TEXT[applies(section, field)]
        app.notify(f"Reset {name} to its default: {value} · {when}", markup=False)

    def _clear_interest(self, field: str) -> None:
        """P15: backspace empties it (about to "", a list to []); an empty one stays as it is."""
        app = self._app
        try:
            interests = reset_interest(app.paths, field)
        except ConfigError as e:
            app.notify(str(e), severity="error", markup=False)
            return
        if interests is None:
            app.notify(f"interests.{field} is already empty", markup=False, timeout=2)
            return
        app.adopt_interests(interests)
        app.notify(f"Cleared interests.{field} · {APPLIES_TEXT['scout']}", markup=False)
