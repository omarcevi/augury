from collections.abc import Sequence
from contextlib import suppress
from typing import ClassVar

from rich.text import Text
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.layout import Layout
from textual.layouts.grid import GridLayout
from textual.widgets import Markdown, MarkdownViewer, Static
from textual.widgets._markdown import MarkdownTable, MarkdownTableContent
from textual.widgets.markdown import MarkdownFence

from augury.core.db.summaries_repo import Summary
from augury.core.models import Item
from augury.core.text import strip_control_chars
from augury.rag.related import RelatedItem
from augury.tui.clipboard import copy_and_tell
from augury.tui.digest_view import breakdown_line
from augury.tui.query import ItemRow
from augury.tui.safe_text import text
from augury.tui.widgets.contents_list import ContentsList

# What the TL;DR box says while one is on its way, and (P12) while none is written yet but one
# could be, which only a collapsed box shows: expanding it asks for one.
SUMMARIZING = "Summarizing…"
NOT_WRITTEN = "Not written yet."


class ScrollingTableContent(MarkdownTableContent):
    """Columns at their natural width, up to 40 cells. Textual's table content is a grid that
    shrinks to fit, cutting every cell of a wide results table to "0.002…"; this one never
    shrinks. The cap makes cells of prose (a "Notes" column) wrap onto a few lines instead of
    running 150 cells wide, so only tables of many columns need to scroll sideways."""

    DEFAULT_CSS = """
    ScrollingTableContent { width: auto; }
    ScrollingTableContent > MarkdownTableCellContents { max-width: 40; }
    """

    def pre_layout(self, layout: Layout) -> None:
        assert isinstance(layout, GridLayout)
        layout.auto_minimum = True
        layout.expand = False
        layout.shrink = False
        layout.stretch_height = True


class ScrollingTable(MarkdownTable):
    """A Markdown table wider than the text column scrolls sideways, like a code block."""

    DEFAULT_CSS = """
    ScrollingTable { overflow-x: auto; scrollbar-size-horizontal: 1; }
    """

    def compose(self) -> ComposeResult:
        headers, rows = self._get_headers_and_rows()
        self._headers, self._rows = headers, rows
        yield ScrollingTableContent(headers, rows)


class ReaderDocument(Markdown):
    """The article: wide tables scroll sideways, and only its own style follows its focus.

    Textual restyles a widget and every one of its descendants whenever the widget gains or
    loses focus. Nothing in the article is styled by that, and a long paper is thousands of
    widgets: a 28k-word one (~3.6k widgets) took about a second per focus change -- esc back
    to the list, clicking the list or the contents, opening an item from the list.
    """

    # Not a ClassVar here: Markdown declares BLOCKS without one (pyright would reject it).
    BLOCKS = dict(Markdown.BLOCKS, table_open=ScrollingTable)  # noqa: RUF012

    def watch_has_focus(self, _has_focus: bool) -> None:
        with suppress(NoActiveAppError):  # as DOMNode.update_node_styles does
            self.app.stylesheet.update_nodes([self], animate=True)


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

    def watch_show_table_of_contents(self, show_table_of_contents: bool) -> None:
        # MarkdownViewer toggles a class on itself, which restyles every widget in the article
        # (about a second for a long paper); an inline display only re-lays it out.
        with suppress(NoMatches):
            self.table_of_contents.display = show_table_of_contents

    def compose(self) -> ComposeResult:
        # MarkdownViewer.compose, with the wrapping ContentsList instead of the one-line Tree
        # (and a document whose focus changes don't restyle the whole article).
        markdown = ReaderDocument(parser_factory=self._parser_factory, open_links=self._open_links)
        markdown.can_focus = True
        yield markdown
        yield ContentsList(markdown)


class ReaderPane(Vertical):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll_lines(1)", "scroll down", show=False),
        Binding("k", "scroll_lines(-1)", "scroll up", show=False),
        # Shadows the app's K (Kind picker) only while the reader has focus.
        Binding("J", "scroll_lines(5)", "fast scroll down", show=False),
        Binding("K", "scroll_lines(-5)", "fast scroll up", show=False),
        Binding("g", "scroll_top", "top", show=False),
        Binding("G", "scroll_bottom", "bottom", show=False),
        Binding("ctrl+d", "half_page(1)", "half page down", show=False),
        Binding("ctrl+u", "half_page(-1)", "half page up", show=False),
        Binding("space", "page_down", "page down", show=False),
        Binding("y", "copy_code", "copy code"),
        Binding("Y", "app.copy_article", "copy article"),
        Binding("c", "toggle_contents", "contents"),
        Binding("left_square_bracket", "prev_section", "prev section"),
        Binding("right_square_bracket", "next_section", "next section"),
        Binding("z", "app.toggle_zen", "zen"),
        Binding("T", "app.retry_tldr", "retry TL;DR", show=False),  # F28; `t` is the theme
        Binding("h", "app.toggle_tldr", "hide/show TL;DR", show=False),  # P12
        Binding("n", "app.next_item", "next"),
        Binding("p", "app.prev_item", "prev"),
        Binding("escape", "app.back_to_table", "back"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status_message = ""
        self.preview_text = ""
        self.tldr_text = ""  # what the TL;DR box shows, as plain text ("" while it's hidden)
        self.tldr_collapsed = False  # P12: the box on one line (AuguryApp sets it, h toggles it)
        self._tldr: tuple[Summary | None, str] = (None, "")  # what show_tldr was last given

    def compose(self) -> ComposeResult:
        yield Static(id="reader-header")
        yield Static(id="reader-status")
        # A Static can't scroll, so the box around it does: a long TL;DR and its takeaways stay
        # reachable (the wheel, or shift+tab from the article and the arrow keys) at up to half
        # the reader's height. It sits outside the article's scroll: reading progress, the
        # contents and [ ] don't see it (AuguryApp re-anchors when its height changes).
        with VerticalScroll(id="reader-tldr-box") as box:
            box.border_title = "TL;DR"
            tldr = Static(id="reader-tldr")
            tldr.display = False
            yield tldr
        yield SafeMarkdownViewer("", show_table_of_contents=False, id="reader-doc")
        related = Static(id="reader-related")  # M4 (spec §6.5), under the article
        related.border_title = "Related"
        yield related

    @property
    def viewer(self) -> MarkdownViewer:
        return self.query_one("#reader-doc", MarkdownViewer)

    def set_reading_width(self, width: int) -> None:
        """Cap the text column at `width` cells ([tui] reading_width); the viewer centres it,
        so in zen (or any reader wider than that) the rest becomes margins."""
        document = self.viewer.document
        document.styles.max_width = width + document.styles.gutter.width

    def show_header(self, item: Item, row: ItemRow | None, also: Sequence[str] = ()) -> None:
        """`also` (M4): the sources of the item's cluster siblings, "Also covered by: …"."""
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
        if also:  # M4 (spec §6.5): grouped, never merged
            header.append("\n")
            header.append_text(text("Also covered by: " + ", ".join(also), "dim"))
        if row is not None and row.why_read:  # F27: triage's one line, from the model
            header.append("\n")
            header.append_text(text(row.why_read, "italic"))
        line = breakdown_line(row.breakdown_json, source_liked=row.source_liked) if row else ""
        if line:  # how the digest scored it
            header.append("\n")
            header.append_text(text(line, "dim"))
        self.query_one("#reader-header", Static).update(header)

    def show_related(self, items: Sequence[RelatedItem], message: str = "") -> None:
        """M4: up to five related items (titles are fetched text: shown literally), a one-line
        reason it is off, or nothing at all (the panel hides)."""
        panel = self.query_one("#reader-related", Static)
        if not items and not message:
            panel.display = False
            return
        body = Text()
        if not items:
            body.append_text(text(message, "dim"))
        for n, item in enumerate(items):
            if n:
                body.append("\n")
            body.append_text(text(f"• {item.title}", one_line=True))
            body.append_text(text(f"  {item.source_id} · {item.similarity:.2f}", "dim"))
        panel.update(body)
        panel.display = True

    def show_status(self, message: str, style: str = "") -> None:
        self.status_message = message
        self.query_one("#reader-status", Static).update(text(message, style))

    def show_tldr(self, summary: Summary | None, message: str = "") -> None:
        """A TL;DR, a one-line status, or nothing at all (the box hides). The bullets come from
        the model, so they go through `text()`: shown literally, never parsed as markup.
        Collapsed (P12), the box keeps its border and title around a single line instead."""
        self._tldr = summary, message
        box = self.query_one("#reader-tldr-box", VerticalScroll)
        tldr = self.query_one("#reader-tldr", Static)
        if summary is None and not message:
            self.tldr_text = ""
            if box.has_focus:  # a hidden box would keep the reader's keys
                self.viewer.document.focus()
            box.display = tldr.display = False
            return
        body = Text()
        if self.tldr_collapsed:
            if summary is not None:
                note = ""
            else:  # a failed or budget-stopped TL;DR isn't written yet either: h asks again
                note = "summarizing… · " if message == SUMMARIZING else "not written yet · "
            body.append_text(text(f"TL;DR ▸  {note}h to show", "dim"))
            if box.has_focus:  # nothing left in it to scroll
                self.viewer.document.focus()
        elif summary is None:
            body.append_text(text(message, "dim"))
        else:
            for bullet in summary.tldr:
                body.append_text(text(f"• {bullet}\n"))
            body.append("Takeaways\n", style="bold")
            for point in summary.takeaways:
                body.append_text(text(f"· {point}\n", "dim"))
            body.rstrip()
        self.tldr_text = body.plain
        tldr.update(body)
        tldr.set_class(self.tldr_collapsed, "collapsed")
        box.display = tldr.display = True
        box.scroll_home(animate=False, immediate=True)  # each TL;DR from its first bullet

    def collapse_tldr(self, collapsed: bool) -> None:
        """P12: the box on one line, or in full again, showing what it was last given."""
        self.tldr_collapsed = collapsed
        self.show_tldr(*self._tldr)

    def show_markdown(self, md: str) -> AwaitComplete:
        # Untrusted page text: Textual passes ESC through, so escape sequences go here.
        return self.viewer.document.update(strip_control_chars(md))

    def show_summary(self, item: Item) -> AwaitComplete:
        self.preview_text = item.summary or "No summary yet."
        return self.show_markdown("\n".join(f"> {line}" for line in self.preview_text.splitlines()))

    def preview(self, item: Item, row: ItemRow | None, also: Sequence[str] = ()) -> None:
        """Database only: moving the cursor must never cost a request."""
        self.show_header(item, row, also)
        self.show_related([])  # M4: shown for an opened item only
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

    def action_half_page(self, direction: int) -> None:
        viewer = self.viewer
        half = max(1, viewer.scrollable_content_region.height // 2)
        viewer.scroll_relative(y=direction * half, animate=False)

    def action_scroll_top(self) -> None:
        self.viewer.scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self.viewer.scroll_end(animate=False)

    def code_block_to_copy(self) -> MarkdownFence | None:
        """The hovered code block, else the one in view nearest the top of the viewport."""
        viewer = self.viewer
        hovered = self.app.mouse_over
        for node in hovered.ancestors_with_self if hovered is not None else ():
            if isinstance(node, MarkdownFence) and viewer in node.ancestors:
                return node
        view = viewer.scrollable_content_region
        in_view = [
            fence
            for fence in viewer.document.query(MarkdownFence)
            if (region := fence.region) and region.y < view.bottom and region.bottom > view.y
        ]
        return min(in_view, key=lambda fence: fence.region.y, default=None)

    def action_copy_code(self) -> None:
        # The whole block, including what's scrolled out of view sideways: the selection only
        # ever sees what's on screen.
        if (fence := self.code_block_to_copy()) is None:
            self.notify("No code block in view", markup=False, timeout=2)
            return
        copy_and_tell(self.app, fence.code, "code block")

    def action_toggle_contents(self) -> None:
        viewer = self.viewer
        viewer.show_table_of_contents = not viewer.show_table_of_contents
        if not viewer.show_table_of_contents and viewer.table_of_contents.has_focus_within:
            viewer.document.focus()  # a hidden list would otherwise keep the keys

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
