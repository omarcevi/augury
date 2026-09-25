from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from augury.tui.keymap import KEYMAP

# These two hints must never scroll off the hints row, whatever width is available.
_ESSENTIAL_LABELS = ("help", "quit")


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
        hints = KEYMAP.get(self.mode, ())
        essential = [h for h in hints if h.label in _ESSENTIAL_LABELS]
        rest = [h for h in hints if h.label not in _ESSENTIAL_LABELS]
        row = Text()
        row.append(f" {self.mode} ", style=f"bold reverse {palette.get('accent', '')}")
        # Essentials come right after the mode badge so a narrow terminal truncates the
        # (less critical) rest of the hints first, never help/quit.
        for hint in (*essential, *rest):
            row.append(f"  {hint.key}:{hint.label}")
        return row
