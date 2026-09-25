from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import Static

from augury.tui.keymap import KEYMAP


class HelpOverlay(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "close"),
        Binding("question_mark", "app.pop_screen", "close"),
        Binding("q", "app.pop_screen", "close"),
    ]

    def compose(self) -> ComposeResult:
        body = Text()
        for mode, hints in KEYMAP.items():
            body.append(f"{mode}\n", style="bold")
            for hint in hints:
                body.append(f"  {hint.key:<10} {hint.label}\n")
        yield Static(body, id="help-body")
