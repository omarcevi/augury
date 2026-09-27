from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from augury.sources.probe import ProbeResult
from augury.sources.rss import FeedInfo
from augury.tui.safe_text import text


class AddSourcePanel(Vertical):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.candidates: list[FeedInfo] = []

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="a blog's URL, a feed URL or a publication's name, then Enter", id="add-url"
        )
        yield Static(id="add-status")
        yield OptionList(id="add-candidates")

    def reset(self) -> None:
        self.candidates = []
        self.query_one("#add-url", Input).value = ""
        self.query_one("#add-status", Static).update("")
        self.query_one(OptionList).clear_options()
        self.query_one("#add-url", Input).focus()

    def show_status(self, message: str) -> None:
        self.query_one("#add-status", Static).update(text(message))

    def show_result(self, result: ProbeResult) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        self.candidates = result.candidates
        if not result.candidates:
            tried = "\n".join(f"  tried {a.url}: {a.outcome}" for a in result.attempts)
            self.show_status("No usable feed found; asking the discovery agent.\n" + tried)
            return
        self.show_status("Enter adds the highlighted feed · Esc cancels")
        for i, c in enumerate(result.candidates):
            label = text(f"{c.title}  ({c.feed_url}, {c.entries} entries)\n", one_line=False)
            for title in c.sample_titles:
                label.append_text(text(f"   · {title}\n", "dim"))
            options.add_option(Option(label, id=str(i)))
        options.highlighted = 0
        options.focus()
