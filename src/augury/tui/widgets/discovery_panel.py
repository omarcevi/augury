"""The discovery panel (spec §8.2): live tool-call progress lines, then a checklist of tested
candidates with their sample titles. space checks, Enter adds the checked ones, Esc cancels."""

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from augury.agents.discovery.agent import DiscoveryOutcome
from augury.agents.discovery.guards import Progress
from augury.agents.discovery.models import Candidate, primary_url
from augury.tui.safe_text import text

MAX_PROGRESS_LINES = 12


class CandidateList(OptionList):
    """A checklist of multi-line options (Textual's SelectionList shows one line per option,
    and each candidate needs its sample titles): space toggles, Enter confirms."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle_candidate", "check", show=False),
        Binding("enter", "confirm", "add checked"),
    ]

    class Confirmed(Message):
        pass

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.candidates: list[Candidate] = []
        self.checked: set[int] = set()

    def show(self, candidates: list[Candidate], checked: set[int]) -> None:
        self.candidates, self.checked = candidates, set(checked)
        self.clear_options()
        self.add_options(
            [
                Option(candidate_label(c, i in self.checked), id=str(i), disabled=c.duplicate)
                for i, c in enumerate(candidates)
            ]
        )

    def action_toggle_candidate(self) -> None:
        i = self.highlighted
        if i is None or self.candidates[i].duplicate:
            return
        self.checked ^= {i}
        self.replace_option_prompt_at_index(
            i, candidate_label(self.candidates[i], i in self.checked)
        )

    def action_confirm(self) -> None:
        self.post_message(self.Confirmed())


def candidate_label(c: Candidate, checked: bool) -> Text:
    label = text("[x] " if checked else "[ ] ", "bold" if checked else "", one_line=False)
    label.append_text(text(f"{c.name}  ", one_line=False))
    label.append_text(text(f"{c.recipe.type} · {primary_url(c.recipe)}", "dim"))
    if c.duplicate_of:
        label.append_text(text(f"  already added as {c.duplicate_of}", "bold"))
    for sample in c.sample_items:
        label.append_text(text(f"\n   · {sample.title}", "dim"))
    return label


class DiscoveryPanel(Vertical):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.candidates: list[Candidate] = []
        self.outcome: DiscoveryOutcome | None = None
        self.rediscover_id: str | None = None  # set while re-discovering a broken source
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(id="discovery-status")
        yield Static(id="discovery-progress")
        yield CandidateList(id="discovery-candidates")

    def start(self, query: str, *, rediscover_id: str | None = None) -> None:
        self.candidates, self.outcome, self.rediscover_id = [], None, rediscover_id
        self._lines = []
        verb = f"Re-discovering {rediscover_id}" if rediscover_id else "Discovering"
        self.show_status(f"{verb}: {query} · Esc cancels")
        self.query_one("#discovery-progress", Static).update("")
        self.query_one(CandidateList).clear_options()

    def show_status(self, message: str) -> None:
        self.query_one("#discovery-status", Static).update(text(message))

    def add_progress(self, line: Progress) -> None:
        if line.done:
            mark = "✓" if line.ok else "✗"
            entry = f"   {mark} {line.detail}" if line.detail else f"   {mark}"
        else:
            entry = f"→ {line.tool} {line.detail}".rstrip()
        self._lines = [*self._lines, entry][-MAX_PROGRESS_LINES:]
        self.query_one("#discovery-progress", Static).update(text("\n".join(self._lines)))

    def show_outcome(self, outcome: DiscoveryOutcome, *, focus: bool = True) -> None:
        self.outcome, self.candidates = outcome, outcome.candidates
        options = self.query_one(CandidateList)
        options.clear_options()
        note = f" · {outcome.explanation}" if outcome.explanation else ""
        if not outcome.candidates:
            self.show_status(f"No candidates{note}")
            return
        if self.rediscover_id:
            how = f"Enter replaces {self.rediscover_id}'s recipe with the first checked"
        else:
            how = "Enter adds the checked"
        self.show_status(f"{len(outcome.candidates)} candidate(s) · space checks · {how}{note}")
        first = next((i for i, c in enumerate(outcome.candidates) if not c.duplicate), None)
        options.show(outcome.candidates, {first} if first is not None else set())
        options.highlighted = first if first is not None else 0
        if focus:  # not while another view is showing: Enter there would add unseen candidates
            options.focus()

    def checked(self) -> list[Candidate]:
        chosen = self.query_one(CandidateList).checked
        return [c for i, c in enumerate(self.candidates) if i in chosen and not c.duplicate]
