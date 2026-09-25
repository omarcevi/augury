from typing import ClassVar

from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Markdown, MarkdownViewer, Static

from augury.core.models import Item
from augury.core.text import strip_control_chars
from augury.tui.query import ItemRow
from augury.tui.safe_text import text


class SafeMarkdownViewer(MarkdownViewer):
    """A MarkdownViewer that never follows a clicked link itself.

    MarkdownViewer treats any clicked href as a local file to read and display (and a web URL
    crashes the app with FileNotFoundError). Extracted pages are untrusted, so a click only
    reaches `Markdown`'s own handler, which passes it to `app.open_url` (http(s) only);
    in-page `#anchor` links still scroll.
    """

    async def _on_markdown_link_clicked(self, message: Markdown.LinkClicked) -> None:
        message.prevent_default()  # skip MarkdownViewer's handler, which loads local files
        message.stop()
        if message.href.startswith("#"):
            self.document.goto_anchor(message.href[1:])


class ReaderPane(Vertical):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll_lines(1)", "scroll down", show=False),
        Binding("k", "scroll_lines(-1)", "scroll up", show=False),
        Binding("space", "page_down", "page down", show=False),
        Binding("c", "toggle_contents", "contents"),
        Binding("left_square_bracket", "prev_section", "prev section"),
        Binding("right_square_bracket", "next_section", "next section"),
        Binding("z", "app.toggle_zen", "zen"),
        Binding("n", "app.next_item", "next"),
        Binding("p", "app.prev_item", "prev"),
        Binding("escape", "app.back_to_table", "back"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status_message = ""
        self.preview_text = ""

    def compose(self) -> ComposeResult:
        yield Static(id="reader-header")
        yield Static(id="reader-status")
        yield SafeMarkdownViewer("", show_table_of_contents=False, id="reader-doc")

    @property
    def viewer(self) -> MarkdownViewer:
        return self.query_one("#reader-doc", MarkdownViewer)

    def show_header(self, item: Item, row: ItemRow | None) -> None:
        header = text(item.title)
        header.stylize("bold")
        meta = [item.source_id, item.kind]
        if item.published_at:
            meta.append(f"{item.published_at:%d %b %Y}")
        if item.arxiv_id:
            meta.append(f"arXiv {item.arxiv_id}")
        if row and row.reading_minutes:
            meta.append(f"{row.reading_minutes} min")
        if row and row.popularity is not None:
            meta.append(f"▲{row.popularity}")
        header.append("\n")
        header.append_text(text(" · ".join(meta), "dim"))
        self.query_one("#reader-header", Static).update(header)

    def show_status(self, message: str, style: str = "") -> None:
        self.status_message = message
        self.query_one("#reader-status", Static).update(text(message, style))

    def show_markdown(self, md: str) -> AwaitComplete:
        # Untrusted page text: Textual passes ESC through, so escape sequences go here.
        return self.viewer.document.update(strip_control_chars(md))

    def show_summary(self, item: Item) -> AwaitComplete:
        self.preview_text = item.summary or "No summary yet."
        return self.show_markdown("\n".join(f"> {line}" for line in self.preview_text.splitlines()))

    def preview(self, item: Item, row: ItemRow | None) -> None:
        """Database only: moving the cursor must never cost a request."""
        self.show_header(item, row)
        self.show_status("Enter to read · o to open in your browser", "dim")
        self.show_summary(item)

    def _heading_offsets(self) -> list[int]:
        document = self.viewer.document
        offsets: list[int] = []
        for _level, _label, block_id in document.table_of_contents or []:
            if block_id:
                try:
                    offsets.append(document.query_one(f"#{block_id}").virtual_region.y)
                except NoMatches:
                    continue
        return offsets

    def action_scroll_lines(self, lines: int) -> None:
        self.viewer.scroll_relative(y=lines, animate=False)

    def action_page_down(self) -> None:
        self.viewer.scroll_page_down(animate=False)

    def action_toggle_contents(self) -> None:
        self.viewer.show_table_of_contents = not self.viewer.show_table_of_contents

    def action_next_section(self) -> None:
        y = self.viewer.scroll_y
        target = next((off for off in self._heading_offsets() if off > y + 1), None)
        if target is not None:
            self.viewer.scroll_to(y=target, animate=False)

    def action_prev_section(self) -> None:
        y = self.viewer.scroll_y
        target = next((off for off in reversed(self._heading_offsets()) if off < y - 1), None)
        if target is not None:
            self.viewer.scroll_to(y=target, animate=False)
