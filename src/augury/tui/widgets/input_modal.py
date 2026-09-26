from collections.abc import Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from augury.tui.safe_text import text


class InputModal(ModalScreen[str | None]):
    """One value typed in. Enter checks it with `check` (a problem, or None): a problem shows
    under the field and the modal stays open, so nothing invalid is ever returned. Esc cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self, title: str, value: str, hint: str, check: Callable[[str], str | None]
    ) -> None:
        super().__init__()
        self._title, self._value, self._hint, self._check = title, value, hint, check

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box") as box:
            box.border_title = text(self._title)
            box.border_subtitle = "enter saves · esc cancels"
            yield Static(text(self._hint), id="input-hint")
            yield Input(value=self._value, id="input-value")
            error = Static(id="input-error")
            error.display = False
            yield error

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the app's own handler is for the search box
        problem = self._check(event.value)
        if problem is None:
            self.dismiss(event.value)
            return
        error = self.query_one("#input-error", Static)
        error.update(text(problem))  # it may quote what was typed: never markup
        error.display = True

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)
