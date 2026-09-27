"""The Ask drawer (spec §8.2), at the bottom of the reader pane: a question box, the answer
with its [n] citations, and the cited passages. Choosing one jumps to it: its section in the
open item, or it opens the item it comes from. Model text is shown literally (safe_text)."""

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from augury.agents.ask import AskAnswer, AskScope, Passage
from augury.tui.safe_text import text


class AskDrawer(Vertical):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "close")]

    class Closed(Message):
        """Esc: the drawer hid itself; the app gives the focus back."""

    class PassageChosen(Message):
        def __init__(self, passage: Passage) -> None:
            super().__init__()
            self.passage = passage

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.scope: AskScope | None = None
        self.answer_text = ""  # what the answer box shows, as plain text (for tests)
        self._shown: list[Passage] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Ask a question, then enter", id="ask-input")
        with VerticalScroll(id="ask-answer-box"):
            yield Static(id="ask-answer")
        yield OptionList(id="ask-sources")

    def open(self, scope: AskScope, title: str) -> None:
        self.scope = scope
        self.border_title = title
        box = self.query_one("#ask-input", Input)
        box.value = ""
        self._show(Text(), [])
        self.display = True
        box.focus()

    def show_status(self, message: str) -> None:
        self._show(text(message, "dim"), [])

    def show_answer(self, answer: AskAnswer, warning_style: str = "") -> None:
        body = text(answer.answer)
        if answer.warning:
            body.append("\n")
            body.append_text(text(answer.warning, warning_style))
        by_n = {p.n: p for p in answer.passages}
        self._show(body, [by_n[n] for n in answer.cited if n in by_n])

    def _show(self, body: Text, passages: list[Passage]) -> None:
        self.answer_text = body.plain
        self.query_one("#ask-answer", Static).update(body)
        sources = self.query_one("#ask-sources", OptionList)
        sources.clear_options()
        self._shown = passages
        for p in passages:
            where = f" · §{p.section}" if p.section else ""
            sources.add_option(
                Option(text(f"[{p.n}] {p.title} · {p.source_id}{where}", one_line=True))
            )
        sources.display = bool(passages)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._shown):
            self.post_message(self.PassageChosen(self._shown[event.option_index]))

    def action_close(self) -> None:
        self.display = False
        self.post_message(self.Closed())
