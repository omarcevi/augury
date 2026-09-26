"""The reader's contents sidebar: one entry per heading, wrapped onto at most two lines.

Textual's `MarkdownTableOfContents` shows a `Tree`, one line per heading, so a long paper
heading was cut to "Add memory to the agent you alr…". This keeps that widget's contract (the
viewer feeds it `table_of_contents` and handles `Markdown.TableOfContentsSelected`), but
draws the headings in an `OptionList` whose entries wrap, indented by heading level.
"""

from textual.app import ComposeResult
from textual.content import Content
from textual.css.styles import RulesMap
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual
from textual.widgets import Markdown, OptionList
from textual.widgets.markdown import MarkdownTableOfContents, TableOfContentsType
from textual.widgets.option_list import Option

from augury.tui.safe_text import text

INDENT = 2  # cells per heading level below the top one
MAX_DEPTH = 3  # deeper levels share the last indent: the sidebar is only ~30 cells wide
MAX_LINES = 2
ELLIPSIS = "…"
NO_SECTIONS = "No sections"


def _label(heading: str) -> Content:
    # Untrusted page text: safe_text strips control characters and never parses markup.
    return Content.from_rich_text(text(" ".join(heading.split())))


class ContentsEntry(Visual):
    """A heading label, indented, wrapped to at most MAX_LINES lines (then an ellipsis)."""

    def __init__(self, label: Content, indent: int) -> None:
        self.label = label
        self.indent = indent
        self._cache: tuple[int, list[Content]] | None = None

    def lines(self, width: int) -> list[Content]:
        if self._cache is not None and self._cache[0] == width:
            return self._cache[1]
        room = max(2, width - self.indent)
        wrapped = [line.rstrip() for line in self.label.wrap(room)] or [Content("")]
        if len(wrapped) > MAX_LINES:
            last = wrapped[MAX_LINES - 1]
            if last.cell_length >= room:
                last = last.truncate(room - 1).rstrip()
            wrapped = [*wrapped[: MAX_LINES - 1], last + ELLIPSIS]
        pad = " " * self.indent
        lines = [pad + line for line in wrapped]
        self._cache = (width, lines)
        return lines

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        return Content("\n").join(self.lines(width)).render_strips(width, height, style, options)

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self.indent + self.label.cell_length

    def get_minimal_width(self, rules: RulesMap) -> int:
        return self.indent + 2

    def get_height(self, rules: RulesMap, width: int) -> int:
        return len(self.lines(width))


class ContentsOptions(OptionList):
    DEFAULT_CSS = """
    ContentsOptions {
        width: auto;
        max-width: 100%;
        height: 1fr;
        max-height: 100%;
        border: none;
        padding: 1 1;
        background: $panel;
        &:focus { border: none; }
    }
    """


class ContentsList(MarkdownTableOfContents):
    """A drop-in for the MarkdownViewer's contents: same data in, same message out."""

    DEFAULT_CSS = """
    ContentsList { width: auto; }
    """

    def compose(self) -> ComposeResult:
        yield ContentsOptions()

    def rebuild_table_of_contents(self, table_of_contents: TableOfContentsType) -> None:
        # A heading with no text (e.g. an arXiv one that was only math) has nothing to show.
        headings = [
            (lvl, name, bid) for lvl, name, bid in table_of_contents if name.strip() and bid
        ]
        # Indent by the levels actually used (arXiv jumps from h2 to h5/h6), a few at most.
        depth = {
            level: min(i, MAX_DEPTH) for i, level in enumerate(sorted({h[0] for h in headings}))
        }
        options = self.query_one(ContentsOptions)
        options.clear_options()
        options.add_options(
            Option(ContentsEntry(_label(name), INDENT * depth[level]), block_id)
            for level, name, block_id in headings
        )
        if not headings:  # rather than an empty strip
            options.add_option(Option(NO_SECTIONS, disabled=True))

    async def on_option_list_option_selected(self, message: OptionList.OptionSelected) -> None:
        message.stop()
        if message.option_id is not None:
            self.post_message(Markdown.TableOfContentsSelected(self.markdown, message.option_id))
