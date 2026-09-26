from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.widgets import DataTable

from augury.tui.query import ItemRow, is_new
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
    # Vim/k9s-style jumps on top of DataTable's own keys (arrows, PageUp/PageDown, Home/End).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("g", "scroll_top", "top", show=False),
        Binding("G", "scroll_bottom", "bottom", show=False),
        Binding("ctrl+d", "half_page(1)", "half page down", show=False),
        Binding("ctrl+u", "half_page(-1)", "half page up", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(cursor_type="row", id=id)
        self.rows_by_key: dict[str, ItemRow] = {}
        self.hidden_columns: frozenset[str] = frozenset()
        self._drawn_with: tuple[datetime, Mapping[str, str]] | None = None
        self._measured: tuple[int, frozenset[str]] | None = None
        # P11: the last visit's start (None on a first launch, when every unread row is new).
        self.new_since: datetime | None = None

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
        if is_new(r, self.new_since):
            # $text-accent, not $accent: that's only 1.4:1 on textual-light's surface.
            marker = f"bold {palette.get('text-accent') or palette.get('accent', '')}"
            title = Text.assemble(("✦ ", marker), title, no_wrap=True, overflow="ellipsis")
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
        self._drawn_with = (now, palette)
        self._measured = (self._title_width(), self.hidden_columns)
        self.clear(columns=True)
        for key, label, width in COLUMNS:
            if key not in self.hidden_columns:
                self.add_column(label, key=key, width=width or self._title_width())
        self.rows_by_key = {r.id: r for r in rows}
        for r in rows:
            self.add_row(*self._cells(r, now, palette), key=r.id)

    def remeasure(self) -> None:
        """Rebuild the columns for the current width and hidden set; same rows, same cursor."""
        if self._drawn_with is None or self._measured == (self._title_width(), self.hidden_columns):
            return
        current = self.current_row()
        with self.prevent(DataTable.RowHighlighted):  # the selection itself doesn't change
            self.show(list(self.rows_by_key.values()), *self._drawn_with)
            if current is not None:
                self.select_key(current.id)

    def on_resize(self, event: events.Resize) -> None:
        # The title column takes whatever width is left, and the real width is only known once
        # the table is laid out (the app's own Resize arrives before that).
        self.remeasure()

    def update_row(self, row: ItemRow, now: datetime, palette: Mapping[str, str]) -> None:
        """Redraw one row in place (e.g. its status dot) without reshuffling the table."""
        if row.id not in self.rows_by_key:
            return
        self.rows_by_key[row.id] = row
        keys = [key for key, _, _ in COLUMNS if key not in self.hidden_columns]
        for key, cell in zip(keys, self._cells(row, now, palette), strict=True):
            self.update_cell(row.id, key, cell)

    def _on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._stop_if_no_row(event)

    def _on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._stop_if_no_row(event)

    @staticmethod
    def _stop_if_no_row(event: DataTable.RowHighlighted | DataTable.RowSelected) -> None:
        # An empty table (first launch, a search with no hits, Show: Saved with nothing saved)
        # still reports its cursor row on g/G, arrows, page keys and Enter -- with no row key.
        # Nothing above should have to handle that, so it goes no further.
        if event.row_key is None:  # typed as a RowKey, but None here
            event.stop()

    def action_half_page(self, direction: int) -> None:
        visible = self.scrollable_content_region.height - (
            self.header_height if self.show_header else 0
        )
        target = self.cursor_row + direction * max(1, visible // 2)
        self.move_cursor(row=max(0, min(target, self.row_count - 1)))

    def current_row(self) -> ItemRow | None:
        if not self.rows_by_key or self.cursor_row < 0:
            return None
        key = list(self.rows_by_key)[min(self.cursor_row, len(self.rows_by_key) - 1)]
        return self.rows_by_key[key]

    def select_key(self, key: str) -> None:
        keys = list(self.rows_by_key)
        if key in keys:
            self.move_cursor(row=keys.index(key))
