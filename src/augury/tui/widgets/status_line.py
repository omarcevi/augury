from rich.cells import cell_len
from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from augury.tui.keymap import KEYMAP, KeyHint

# These hints must never scroll off the hints row, whatever width is available ("back" is
# the only way out of the full-screen reader on a narrow terminal, "items" out of Sources).
_ESSENTIAL_LABELS = ("help", "quit", "back", "items")


def essentials_first(hints: tuple[KeyHint, ...]) -> list[KeyHint]:
    essential = [h for h in hints if h.label in _ESSENTIAL_LABELS]
    return essential + [h for h in hints if h.label not in _ESSENTIAL_LABELS]


class StatusLine(Widget):
    mode: reactive[str] = reactive("NORMAL")
    selection: reactive[Text] = reactive(Text)

    def render(self) -> Text:
        # Textual 8.x renders this via its own Content/Visual pipeline (Content.from_rich_text
        # drops Text.no_wrap/overflow entirely), so the "one row per physical line, ellipsize
        # on overflow" guarantee actually comes from `#status { text-wrap: nowrap;
        # text-overflow: ellipsis }` in theme.tcss, not from these Text attributes -- they're
        # set anyway so a plain `Console.print()`/`.render()` caller gets the same behavior.
        # `#status { height: 2 }` then caps the two physical lines (selection, then hints) to
        # exactly what's visible.
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append_text(self.selection)
        line.append("\n")
        line.append_text(self._hints())
        return line

    def _hints(self) -> Text:
        palette = self.app.get_css_variables()
        row = Text()
        row.append(f" {self.mode} ", style=f"bold reverse {palette.get('accent', '')}")
        # Essentials come right after the mode badge so a narrow terminal truncates the
        # (less critical) rest of the hints first, never help/quit.
        for hint in essentials_first(KEYMAP.get(self.mode, ())):
            piece = f"  {hint.key}:{hint.label}"
            # Whole hints only: one that doesn't fit is left out rather than cut to "y:co…"
            # (and so are the rest, which matter less). The essentials always stay.
            too_wide = self.size.width and row.cell_len + cell_len(piece) > self.size.width
            if too_wide and hint.label not in _ESSENTIAL_LABELS:
                break
            row.append(piece)
        return row
