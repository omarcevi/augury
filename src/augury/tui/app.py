import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from augury.core.clock import utcnow
from augury.core.config import Config
from augury.core.paths import AppPaths
from augury.tui.health import load_health
from augury.tui.keymap import THEMES
from augury.tui.query import DIGEST_PRESET, ItemFilter, list_items
from augury.tui.safe_text import text
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.status_line import StatusLine

EMPTY_MESSAGE = "Nothing here yet. Run `augury scout` to fetch today's items."


class AuguryApp(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "augury"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("question_mark", "help", "help"),
        Binding("t", "cycle_theme", "theme"),
        Binding("q", "quit", "quit"),
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

    def compose(self) -> ComposeResult:
        yield HealthBar(id="health")
        with Container(id="main"), Container(id="items-pane"):
            yield ItemsTable(id="items")
            yield Static(EMPTY_MESSAGE, id="empty")
        yield StatusLine(id="status")

    def on_mount(self) -> None:
        wanted = self.config.tui.theme
        self.theme = wanted if wanted in self.available_themes else "textual-dark"
        self.refresh_health()
        self.reload_items()

    def refresh_health(self, *, scouting: bool = False) -> None:
        self.query_one(HealthBar).snapshot = load_health(self.conn, self.now(), scouting=scouting)

    def refresh_colors(self) -> None:
        """Widgets that bake theme colors into Rich text redraw after a theme change."""
        for widget in (*self.query(HealthBar), *self.query(StatusLine)):
            widget.refresh()
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
