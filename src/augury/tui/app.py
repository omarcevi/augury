import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.reactive import reactive
from textual.widgets import Static

from augury.core.clock import utcnow
from augury.core.config import Config
from augury.core.paths import AppPaths
from augury.tui.health import load_health
from augury.tui.keymap import THEMES
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.status_line import StatusLine


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

    def compose(self) -> ComposeResult:
        yield HealthBar(id="health")
        yield Container(Static("Items arrive in F15.", id="placeholder"), id="main")
        yield StatusLine(id="status")

    def on_mount(self) -> None:
        wanted = self.config.tui.theme
        self.theme = wanted if wanted in self.available_themes else "textual-dark"
        self.refresh_health()

    def refresh_health(self, *, scouting: bool = False) -> None:
        self.query_one(HealthBar).snapshot = load_health(self.conn, self.now(), scouting=scouting)

    def refresh_colors(self) -> None:
        """Widgets that bake theme colors into Rich text redraw after a theme change."""
        for widget in (*self.query(HealthBar), *self.query(StatusLine)):
            widget.refresh()

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
