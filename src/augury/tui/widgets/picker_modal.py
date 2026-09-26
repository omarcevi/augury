from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import OptionList, SelectionList
from textual.widgets.option_list import Option


class PickerModal(ModalScreen[frozenset[str] | None]):
    """Multi-select. Space toggles, Enter applies, Esc cancels. Nothing selected means "all"."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "apply", "apply", priority=True),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(
        self, title: str, options: Sequence[tuple[str | Text, str]], selected: frozenset[str]
    ) -> None:
        """A label that isn't ours (a tag from the model, say) must be a Text: a str is markup."""
        super().__init__()
        self._title, self._options, self._selected = title, options, selected

    def compose(self) -> ComposeResult:
        picker: SelectionList[str] = SelectionList(
            *[(label, value, value in self._selected) for label, value in self._options],
            id="picker",
        )
        picker.border_title = f"{self._title} · space toggles · enter applies"
        yield picker

    def on_mount(self) -> None:
        picker = self.query_one(SelectionList)
        picker.focus()
        if self._options:
            picker.highlighted = 0  # so space toggles the first option straight away

    def action_apply(self) -> None:
        self.dismiss(frozenset(self.query_one(SelectionList).selected))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]], current: str) -> None:
        super().__init__()
        self._title, self._options, self._current = title, options, current

    def compose(self) -> ComposeResult:
        choices = OptionList(
            *[Option(label, id=value) for label, value in self._options], id="choice"
        )
        choices.border_title = self._title
        yield choices

    def on_mount(self) -> None:
        values = [value for _, value in self._options]
        self.query_one(OptionList).highlighted = (
            values.index(self._current) if self._current in values else 0
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
