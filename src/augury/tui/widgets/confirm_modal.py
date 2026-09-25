from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import Static

from augury.tui.safe_text import text


class ConfirmModal(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "yes"),
        Binding("n", "answer(False)", "no"),
        Binding("escape", "answer(False)", "no"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        body = text(self._message)
        body.append("\n\n[y] yes   [n] no", style="bold")
        yield Static(body, id="confirm-body")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)
