from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from augury.tui.keymap import KEYMAP


class StatusLine(Widget):
    mode: reactive[str] = reactive("NORMAL")
    selection: reactive[Text] = reactive(Text)

    def render(self) -> Text:
        palette = self.app.get_css_variables()
        line = Text()
        line.append_text(self.selection)
        line.append("\n")
        line.append(f" {self.mode} ", style=f"bold reverse {palette.get('accent', '')}")
        for hint in KEYMAP.get(self.mode, ()):
            line.append(f"  {hint.key}:{hint.label}")
        return line
