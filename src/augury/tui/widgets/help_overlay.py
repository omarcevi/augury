from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from augury.tui.keymap import KEYMAP
from augury.tui.widgets.status_line import essentials_first


class HelpOverlay(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "close"),
        Binding("question_mark", "app.pop_screen", "close"),
        Binding("q", "app.pop_screen", "close"),
        Binding("j", "scroll_lines(1)", "scroll down", show=False),
        Binding("k", "scroll_lines(-1)", "scroll up", show=False),
    ]

    def __init__(self, mode: str = "NORMAL") -> None:
        super().__init__()
        self.mode = mode

    def compose(self) -> ComposeResult:
        body = Text()
        for mode in sorted(KEYMAP, key=lambda m: m != self.mode):  # the current mode first
            body.append(f"{mode}\n", style="bold")
            for hint in essentials_first(KEYMAP[mode]):
                body.append(f"  {hint.key:<10} {hint.label}\n")
        body.rstrip()
        with VerticalScroll(id="help-body") as scroll:
            scroll.border_title = "Help"
            scroll.border_subtitle = "↑↓ j k scroll · esc close"
            yield Static(body, id="help-text")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()  # so the arrow and page keys scroll it

    def action_scroll_lines(self, lines: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=lines, animate=False)
