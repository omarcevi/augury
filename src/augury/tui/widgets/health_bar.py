from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from augury.tui.health import HealthSnapshot, health_line


class HealthBar(Widget):
    snapshot: reactive[HealthSnapshot | None] = reactive(None)

    def render(self) -> Text:
        if self.snapshot is None:
            return Text("")
        return health_line(self.snapshot, self.app.get_css_variables())
