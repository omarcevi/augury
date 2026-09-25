from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static

from augury.tui.query import ItemFilter
from augury.tui.safe_text import text

DATE_LABELS = {"today": "Today", "7d": "7 days", "30d": "30 days", "all": "All time"}
SORT_LABELS = {"newest": "Newest ↓", "popular": "Popular ↓", "reading_time": "Shortest ↑"}
SHOW_LABELS = {
    "unread": "Unread",
    "all": "All",
    "saved": "Saved",
    "liked": "Liked",
    "hidden": "Hidden",
}


def selection_label(values: frozenset[str]) -> str:
    if not values:
        return "All"
    return next(iter(values)) if len(values) == 1 else f"{len(values)} selected"


class Chip(Static):
    def __init__(self, label: str, key: str, *, id: str) -> None:
        super().__init__(id=id, classes="chip")
        self.border_title = f"{label} ({key})"
        self.value = ""

    def set_value(self, value: str) -> None:
        self.value = value
        self.update(text(value, one_line=True))


class FilterChips(Horizontal):
    def compose(self) -> ComposeResult:
        search = Input(placeholder="type to filter", id="search")
        search.border_title = "Search (/)"
        yield search
        yield Chip("Sources", "S", id="chip-sources")
        yield Chip("Kind", "K", id="chip-kind")
        yield Chip("Date", "D", id="chip-date")
        yield Chip("Sort", "s", id="chip-sort")
        yield Chip("Show", "v", id="chip-show")
        yield Chip("Theme", "t", id="chip-theme")

    def show_filter(self, f: ItemFilter, theme: str) -> None:
        self.query_one("#chip-sources", Chip).set_value(selection_label(f.sources))
        self.query_one("#chip-kind", Chip).set_value(selection_label(f.kinds))
        self.query_one("#chip-date", Chip).set_value(DATE_LABELS[f.date])
        self.query_one("#chip-sort", Chip).set_value(SORT_LABELS[f.sort])
        self.query_one("#chip-show", Chip).set_value(SHOW_LABELS[f.show])
        self.query_one("#chip-theme", Chip).set_value(theme)
