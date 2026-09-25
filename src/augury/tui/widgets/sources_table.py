from collections.abc import Mapping
from datetime import datetime

from rich.text import Text
from textual.widgets import DataTable

from augury.core.db.sources_repo import SourceRecord
from augury.tui.health import humanize_ago
from augury.tui.safe_text import text

HEALTH_MARKS = {
    "never": ("·", "dim"),
    "ok": ("✓", "success"),
    "degraded": ("⚠", "warning"),
    "broken": ("✗", "error"),
}


class SourcesTable(DataTable[Text]):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(cursor_type="row", id=id)
        self.records: list[SourceRecord] = []

    def show(
        self,
        records: list[SourceRecord],
        counts: Mapping[str, int],
        now: datetime,
        palette: Mapping[str, str],
    ) -> None:
        self.clear(columns=True)
        for label in ("St", "ID", "Type", "On", "Last success", "Items", "Last error"):
            self.add_column(label, key=label)
        self.records = records
        for r in records:
            mark, color = HEALTH_MARKS[r.health]
            self.add_row(
                Text(mark, style=palette.get(color, color)),
                text(r.source.id, one_line=True),
                Text(r.source.recipe.type, style="dim"),
                Text("on" if r.source.enabled else "off"),
                Text(humanize_ago(r.last_success_at, now) if r.last_success_at else "never"),
                Text(str(counts.get(r.source.id, 0)), justify="right"),
                text((r.last_error or "")[:50], "dim", one_line=True),
                key=r.source.id,
            )

    def current(self) -> SourceRecord | None:
        if not self.records:
            return None
        return self.records[min(max(self.cursor_row, 0), len(self.records) - 1)]

    def select_key(self, source_id: str) -> None:
        ids = [r.source.id for r in self.records]
        if source_id in ids:
            self.move_cursor(row=ids.index(source_id))
