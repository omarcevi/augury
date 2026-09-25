import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import DataTable, Input, Static

from augury.core.clock import utcnow
from augury.core.config import Config
from augury.core.db.sources_repo import SourcesRepo
from augury.core.paths import AppPaths
from augury.tui.health import load_health
from augury.tui.keymap import THEMES
from augury.tui.query import (
    DATE_RANGES,
    DIGEST_PRESET,
    SHOW_KEYS,
    SORT_KEYS,
    ItemFilter,
    list_items,
)
from augury.tui.safe_text import text
from augury.tui.widgets.filter_chips import DATE_LABELS, FilterChips
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import ChoiceModal, PickerModal
from augury.tui.widgets.status_line import StatusLine

EMPTY_MESSAGE = "Nothing here yet. Run `augury scout` to fetch today's items."


class AuguryApp(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "augury"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("question_mark", "help", "help"),
        Binding("t", "cycle_theme", "theme"),
        Binding("q", "quit", "quit"),
        Binding("slash", "focus_search", "search"),
        Binding("S", "pick_sources", "sources"),
        Binding("K", "pick_kinds", "kind"),
        Binding("D", "pick_date", "date"),
        Binding("s", "cycle_sort", "sort"),
        Binding("v", "cycle_show", "show"),
        Binding("escape", "back_to_table", "back"),
    ]
    mode: reactive[str] = reactive("NORMAL")

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        config: Config,
        paths: AppPaths,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__()
        self.conn, self.config, self.paths, self.now = conn, config, paths, now
        self.item_filter: ItemFilter = DIGEST_PRESET
        self._search_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield HealthBar(id="health")
        yield FilterChips(id="filters")
        with Container(id="main"), Container(id="items-pane"):
            yield ItemsTable(id="items")
            yield Static(EMPTY_MESSAGE, id="empty")
        yield StatusLine(id="status")

    def on_mount(self) -> None:
        wanted = self.config.tui.theme
        self.theme = wanted if wanted in self.available_themes else "textual-dark"
        self.refresh_health()
        self.reload_items()
        self.query_one(FilterChips).show_filter(self.item_filter, self.theme)
        # Without this, Textual auto-focuses the first focusable widget in DOM order,
        # which is the search Input (it comes before the table) -- so a bare "/" or
        # "S" keypress would be swallowed as text instead of reaching the app bindings.
        self.query_one(ItemsTable).focus()

    def refresh_health(self, *, scouting: bool = False) -> None:
        self.query_one(HealthBar).snapshot = load_health(self.conn, self.now(), scouting=scouting)

    def refresh_colors(self) -> None:
        """Widgets that bake theme colors into Rich text redraw after a theme change."""
        for widget in (*self.query(HealthBar), *self.query(StatusLine)):
            widget.refresh()
        self.query_one(FilterChips).show_filter(self.item_filter, self.theme)
        self.reload_items()

    def reload_items(self, *, keep: str | None = None) -> None:
        rows, total = list_items(self.conn, self.item_filter, now=self.now())
        table = self.query_one(ItemsTable)
        table.show(rows, self.now(), self.get_css_variables())
        self.query_one("#items-pane").border_title = f"Items ({len(rows)}/{total})"
        self.query_one("#empty").display = not rows
        table.display = bool(rows)
        if keep:
            table.select_key(keep)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row = self.query_one(ItemsTable).rows_by_key.get(str(event.row_key.value))
        status = self.query_one(StatusLine)
        status.selection = (
            text(f"▶ {row.title}  {row.source_id}", one_line=True) if row else text("")
        )

    def on_resize(self) -> None:
        # The title column takes the remaining width, so re-measure after a resize.
        current = self.query_one(ItemsTable).current_row()
        self.reload_items(keep=current.id if current else None)

    def watch_mode(self, mode: str) -> None:
        for status in self.query(StatusLine):
            status.mode = mode

    def action_help(self) -> None:
        self.push_screen(HelpOverlay())

    def action_cycle_theme(self) -> None:
        names = [name for name in THEMES if name in self.available_themes]
        index = names.index(self.theme) if self.theme in names else -1
        self.theme = names[(index + 1) % len(names)]
        self.refresh_colors()

    def apply_filter(self, f: ItemFilter) -> None:
        self.item_filter = f
        self.query_one(FilterChips).show_filter(f, self.theme)
        self.reload_items()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()
        self.mode = "SEARCH"

    def action_back_to_table(self) -> None:
        self.query_one(ItemsTable).focus()
        self.mode = "NORMAL"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        value = event.value
        self._search_timer = self.set_timer(
            0.15, lambda: self.apply_filter(replace(self.item_filter, search=value))
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.action_back_to_table()

    def action_cycle_sort(self) -> None:
        nxt = SORT_KEYS[(SORT_KEYS.index(self.item_filter.sort) + 1) % len(SORT_KEYS)]
        self.apply_filter(replace(self.item_filter, sort=nxt))

    def action_cycle_show(self) -> None:
        nxt = SHOW_KEYS[(SHOW_KEYS.index(self.item_filter.show) + 1) % len(SHOW_KEYS)]
        self.apply_filter(replace(self.item_filter, show=nxt))

    def action_pick_sources(self) -> None:
        options = [(r.source.id, r.source.id) for r in SourcesRepo(self.conn).list_all()]

        def done(chosen: frozenset[str] | None) -> None:
            if chosen is not None:
                self.apply_filter(replace(self.item_filter, sources=chosen))

        self.push_screen(PickerModal("Sources", options, self.item_filter.sources), done)

    def action_pick_kinds(self) -> None:
        def done(chosen: frozenset[str] | None) -> None:
            if chosen is not None:
                self.apply_filter(replace(self.item_filter, kinds=chosen))

        options = [("Papers", "paper"), ("Articles", "article")]
        self.push_screen(PickerModal("Kind", options, self.item_filter.kinds), done)

    def action_pick_date(self) -> None:
        def done(chosen: str | None) -> None:
            if chosen in DATE_RANGES:
                self.apply_filter(replace(self.item_filter, date=chosen))  # type: ignore[arg-type]

        options = [(DATE_LABELS[d], d) for d in DATE_RANGES]
        self.push_screen(ChoiceModal("Date", options, self.item_filter.date), done)
