from collections.abc import Mapping
from datetime import datetime, timedelta

from rich.text import Text
from textual.widgets import DataTable

from augury.tui.query import ItemRow
from augury.tui.safe_text import text

# (key, label, fixed width); the title column takes whatever width is left.
COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("st", "St", 2),
    ("title", "Title", None),
    ("source", "Source", 14),
    ("pop", "▲", 5),
    ("age", "Age", 4),
    ("min", "Min", 4),
)


def humanize_age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 60 * 24:
        return f"{minutes // 60}h"
    days = minutes // (60 * 24)
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    return f"{days // 30}mo"


class ItemsTable(DataTable[Text]):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(cursor_type="row", id=id)
        self.rows_by_key: dict[str, ItemRow] = {}
        self.hidden_columns: frozenset[str] = frozenset()

    def _title_width(self) -> int:
        fixed = sum((w or 0) + 2 for key, _, w in COLUMNS if w and key not in self.hidden_columns)
        return max(20, (self.size.width or 120) - fixed - 4)

    def _cells(self, r: ItemRow, now: datetime, palette: Mapping[str, str]) -> list[Text]:
        dot, color = {
            "unread": ("●", palette.get("accent", "")),
            "partial": ("◐", palette.get("warning", "")),
            "read": ("○", "dim"),
        }[r.read_state]
        title = text(r.title, one_line=True)
        if r.liked:
            title.append(" ★", style=palette.get("success", ""))
        if r.saved:
            title.append(" ⊕", style=palette.get("accent", ""))
        cells = {
            "st": Text(dot, style=color),
            "title": title,
            "source": text(r.source_id, "dim", one_line=True),
            "pop": Text("—" if r.popularity is None else str(r.popularity), justify="right"),
            "age": Text(
                "↺" if r.is_old else humanize_age(now - (r.published_at or r.first_seen)),
                justify="right",
            ),
            "min": Text(str(r.reading_minutes) if r.reading_minutes else "—", justify="right"),
        }
        return [cells[key] for key, _, _ in COLUMNS if key not in self.hidden_columns]

    def show(self, rows: list[ItemRow], now: datetime, palette: Mapping[str, str]) -> None:
        self.clear(columns=True)
        for key, label, width in COLUMNS:
            if key not in self.hidden_columns:
                self.add_column(label, key=key, width=width or self._title_width())
        self.rows_by_key = {r.id: r for r in rows}
        for r in rows:
            self.add_row(*self._cells(r, now, palette), key=r.id)

    def current_row(self) -> ItemRow | None:
        if not self.rows_by_key or self.cursor_row < 0:
            return None
        key = list(self.rows_by_key)[min(self.cursor_row, len(self.rows_by_key) - 1)]
        return self.rows_by_key[key]

    def select_key(self, key: str) -> None:
        keys = list(self.rows_by_key)
        if key in keys:
            self.move_cursor(row=keys.index(key))
