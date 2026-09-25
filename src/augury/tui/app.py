import asyncio
import sqlite3
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import ClassVar
from urllib.parse import urlsplit

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal
from textual.geometry import Size
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import ContentSwitcher, DataTable, Input, Static

from augury.agents.scout import ScoutDeps, recover_interrupted_runs, run_scout
from augury.core.clock import utcnow
from augury.core.config import Config, HttpConfig
from augury.core.db.items_repo import ItemsRepo
from augury.core.db.runs_repo import RunsRepo
from augury.core.db.sources_repo import SourcesRepo
from augury.core.db.state_repo import StateRepo, Toggle
from augury.core.lock import ScoutAlreadyRunning
from augury.core.paths import AppPaths
from augury.extract.service import get_or_extract
from augury.sources.http import HttpClient, PoliteClient
from augury.tui.health import load_health
from augury.tui.keymap import THEMES
from augury.tui.layout import HIDDEN_COLUMNS, layout_for
from augury.tui.query import (
    DATE_RANGES,
    DIGEST_PRESET,
    SHOW_KEYS,
    SORT_KEYS,
    ItemFilter,
    ItemRow,
    get_item_row,
    list_items,
)
from augury.tui.safe_text import text
from augury.tui.widgets.filter_chips import DATE_LABELS, FilterChips
from augury.tui.widgets.health_bar import HealthBar
from augury.tui.widgets.help_overlay import HelpOverlay
from augury.tui.widgets.items_table import ItemsTable
from augury.tui.widgets.picker_modal import ChoiceModal, PickerModal
from augury.tui.widgets.reader_pane import ReaderPane
from augury.tui.widgets.sources_view import SourcesView
from augury.tui.widgets.status_line import StatusLine

EMPTY_MESSAGE = "Nothing here yet. Run `augury scout` to fetch today's items."
_OPENABLE_SCHEMES = ("http", "https")
# Item-list actions that must not fall through to the (hidden) ItemsTable/reader while the
# Sources view is showing. Unlike `t` (theme vs. test fetch) and `escape` (back vs. close-add),
# nothing in SourcesView shadows these keys, so without this an app-level `l`/`x`/`o`/... would
# act on whatever item was last selected/read in the items view.
_ITEMS_ONLY_ACTIONS = frozenset(
    {
        "toggle_like",
        "toggle_save",
        "toggle_hide",
        "open_browser",
        "pick_sources",
        "pick_kinds",
        "pick_date",
        "cycle_sort",
        "cycle_show",
        "focus_search",
    }
)


class AuguryApp(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "augury"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("question_mark", "help", "help"),
        Binding("t", "cycle_theme", "theme"),
        Binding("q", "quit", "quit"),
        Binding("slash", "focus_search", "search"),
        Binding("1", "show_items", "items"),
        Binding("2", "show_sources", "sources"),
        Binding("S", "pick_sources", "sources"),
        Binding("K", "pick_kinds", "kind"),
        Binding("D", "pick_date", "date"),
        Binding("s", "cycle_sort", "sort"),
        Binding("v", "cycle_show", "show"),
        Binding("l", "toggle_like", "like"),
        Binding("b", "toggle_save", "save"),
        Binding("x", "toggle_hide", "hide"),
        Binding("o", "open_browser", "browser"),
        Binding("r", "scout", "refresh"),
        Binding("escape", "back_to_table", "back"),
    ]
    mode: reactive[str] = reactive("NORMAL")

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        config: Config,
        paths: AppPaths,
        now: Callable[[], datetime] = utcnow,
        http_factory: Callable[[HttpConfig], HttpClient] = PoliteClient,
    ) -> None:
        super().__init__()
        self.conn, self.config, self.paths, self.now = conn, config, paths, now
        self.item_filter: ItemFilter = DIGEST_PRESET
        self._search_timer: Timer | None = None
        self.http_factory = http_factory
        self.http: HttpClient | None = None
        self.reading_id: str | None = None
        self._saved_progress = 0.0
        # The reading position as a fraction, and the max_scroll_y it was taken at (-1 while a
        # re-layout is being re-anchored, so nothing is recorded until it has landed).
        self._scroll_frac = 0.0
        self._scroll_max = -1.0
        self._extracting: str | None = None

    def compose(self) -> ComposeResult:
        yield HealthBar(id="health")
        yield FilterChips(id="filters")
        with ContentSwitcher(id="views", initial="main"):
            with Horizontal(id="main"):
                with Container(id="items-pane"):
                    yield ItemsTable(id="items")
                    yield Static(EMPTY_MESSAGE, id="empty")
                yield ReaderPane(id="reader")
            yield SourcesView(id="sources-view")
        yield StatusLine(id="status")

    def on_mount(self) -> None:
        self.http = self.http_factory(self.config.http)
        self.http.on_wait = self.on_http_wait
        viewer = self.query_one(ReaderPane).viewer
        self.watch(viewer, "scroll_y", self._on_reader_scroll, init=False)
        self.watch(viewer, "virtual_size", self._on_reader_relayout, init=False)
        self.apply_layout(self.size.width)
        wanted = self.config.tui.theme
        self.theme = wanted if wanted in self.available_themes else "textual-dark"
        self.refresh_health()
        self.reload_items()
        self.query_one(FilterChips).show_filter(self.item_filter, self.theme)
        self.query_one("#add-source").display = False
        # Without this, Textual auto-focuses the first focusable widget in DOM order,
        # which is the search Input (it comes before the table) -- so a bare "/" or
        # "S" keypress would be swallowed as text instead of reaching the app bindings.
        self.query_one(ItemsTable).focus()
        self.maybe_auto_scout()

    def maybe_auto_scout(self) -> None:
        recover_interrupted_runs(self.conn, self.paths.scout_lock_file, now=self.now())
        hours = self.config.scout.auto_after_hours
        if not hours:
            return
        last = RunsRepo(self.conn).last("scout", statuses=("ok", "partial"))
        if last is None or self.now() - last.started_at > timedelta(hours=hours):
            self.start_scout()

    def action_scout(self) -> None:
        self.start_scout()

    @work(exclusive=True, group="scout")
    async def start_scout(self) -> None:
        if self.http is None:
            return
        self.refresh_health(scouting=True)
        deps = ScoutDeps(
            conn=self.conn,
            http=self.http,
            config=self.config,
            lock_path=self.paths.scout_lock_file,
            now=self.now,
        )
        try:
            report = await run_scout(deps)
        except asyncio.CancelledError:
            # The app is shutting down (e.g. quit mid-scout): run_scout()'s own `with
            # ScoutLock(...)` already released the lock while unwinding, and touching any
            # widget below would raise NoMatches against a DOM that's being (or has been)
            # torn down -- Textual then treats that as a fatal worker error. Just re-raise
            # so the worker reports itself cancelled, not failed.
            raise
        except ScoutAlreadyRunning:
            self.notify(
                "A scout is already running (maybe the scheduled one).",
                severity="warning",
                markup=False,
            )
        except Exception as exc:  # the app stays usable with whatever is cached
            self.notify(
                f"Scout failed: {type(exc).__name__}: {exc}", severity="error", markup=False
            )
        else:
            if report.status != "ok":
                failed = ", ".join(sid for sid, s in report.sources.items() if s.error)
                self.notify(
                    f"Scout {report.status}: {failed} failed", severity="warning", markup=False
                )
        finally:
            # Guard against the app shutting down mid-scout: once `is_running` is False,
            # Textual has begun (or finished) pruning the DOM, and any widget query below
            # would raise NoMatches instead of the CancelledError actually in flight.
            if self.is_running:
                self.refresh_health()
                current = self.query_one(ItemsTable).current_row()
                self.reload_items(keep=current.id if current else None)
                if self.mode == "SOURCES":
                    self.refresh_sources()

    def refresh_health(self, *, scouting: bool = False) -> None:
        self.query_one(HealthBar).snapshot = load_health(self.conn, self.now(), scouting=scouting)

    def refresh_colors(self) -> None:
        """Widgets that bake theme colors into Rich text redraw after a theme change."""
        for widget in (*self.query(HealthBar), *self.query(StatusLine)):
            widget.refresh()
        self.query_one(FilterChips).show_filter(self.item_filter, self.theme)
        self.reload_items()

    def reload_items(self, *, keep: str | None = None) -> None:
        # While reading, the open item stays listed and selected, whatever the filter says.
        rows, total = list_items(
            self.conn, self.item_filter, now=self.now(), pinned=self.reading_id
        )
        table = self.query_one(ItemsTable)
        table.show(rows, self.now(), self.get_css_variables())
        self.query_one("#items-pane").border_title = f"Items ({len(rows)}/{total})"
        self.query_one("#empty").display = not rows
        table.display = bool(rows)
        if keep := keep or self.reading_id:
            table.select_key(keep)

    async def on_unmount(self) -> None:
        close = getattr(self.http, "aclose", None)
        if close is not None:
            await close()

    def apply_layout(self, width: int) -> None:
        layout = layout_for(width)
        table = self.query_one(ItemsTable)
        for name in ("wide", "medium", "narrow"):  # the table's screen, even behind a modal
            table.screen.set_class(name == layout, f"layout-{name}")
        table.hidden_columns = HIDDEN_COLUMNS[layout]
        table.remeasure()

    def on_resize(self, event: events.Resize) -> None:
        # No re-query: that would drop an item opened from the Unread view. The table
        # re-measures its title column on its own Resize, once it has been laid out.
        self.apply_layout(event.size.width)
        self._reanchor()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "items":
            return
        row = self.query_one(ItemsTable).rows_by_key.get(str(event.row_key.value))
        status = self.query_one(StatusLine)
        status.selection = (
            text(f"▶ {row.title}  {row.source_id}", one_line=True) if row else text("")
        )
        if row and self.reading_id is None and (item := ItemsRepo(self.conn).get(row.id)):
            self.query_one(ReaderPane).preview(item, row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "items":
            return
        self.open_item(str(event.row_key.value))

    def open_item(self, item_id: str) -> None:
        viewer = self.query_one(ReaderPane).viewer
        self.reading_id = None  # the old document scrolls back to the top unrecorded
        viewer.scroll_home(animate=False, immediate=True)
        progress = StateRepo(self.conn).get(item_id).read_progress
        self.reading_id, self._saved_progress = item_id, progress
        self._scroll_frac = progress if progress < 0.9 else 0.0  # finished items reopen at the top
        self._scroll_max = -1.0
        self.screen.add_class("reading")
        self.mode = "READ"
        # MarkdownViewer itself can't take focus; its document can, so the reader keys work.
        viewer.document.focus()
        self.load_content(item_id)

    @work(exclusive=True, group="reader")
    async def load_content(self, item_id: str) -> None:
        await asyncio.sleep(0.3)  # debounce: pressing Enter again cancels this before any request
        reader = self.query_one(ReaderPane)
        item = ItemsRepo(self.conn).get(item_id)
        if item is None or self.http is None or self.reading_id != item_id:
            return
        # Only now: items skipped past with n/p inside the debounce stay unread.
        StateRepo(self.conn).mark_opened(item_id, now=self.now())
        self._refresh_row(item_id)
        reader.show_header(item, self.query_one(ItemsTable).rows_by_key.get(item_id))
        reader.show_status("Extracting…", "dim")
        # Awaited: Markdown resets its cached table of contents when an update starts, so an
        # update still running when the body arrives would cache a contents list without it.
        await reader.show_summary(item)
        self._extracting = item_id
        try:
            content = await get_or_extract(self.conn, self.http, item, now=self.now())
        except Exception as exc:  # a reader problem must never take the whole app down
            self.log.error(f"reader: extracting {item_id} failed\n{traceback.format_exc()}")
            if self.reading_id == item_id:
                reader.show_status(
                    f"Something went wrong ({type(exc).__name__}). "
                    "Press o to open it in your browser.",
                    "bold",
                )
            return
        finally:
            if self._extracting == item_id:
                self._extracting = None
        self._refresh_row(item_id)
        if self.reading_id != item_id:  # the reader was closed meanwhile; the result is cached
            return
        # Again, now that the row knows the reading time.
        reader.show_header(item, self.query_one(ItemsTable).rows_by_key.get(item_id))
        if content.status == "ok":
            reader.show_status("")
            await reader.show_markdown(content.body_md)
            self._reanchor()  # back to the saved position once the body is laid out
        else:
            reader.show_status(
                f"Couldn't extract this item ({content.error or content.status}). "
                "Press o to open it in your browser.",
                self.get_css_variables().get("warning", ""),
            )

    def _refresh_row(self, item_id: str) -> None:
        if (row := get_item_row(self.conn, item_id, now=self.now())) is not None:
            self.query_one(ItemsTable).update_row(row, self.now(), self.get_css_variables())

    def _on_reader_relayout(self, _size: Size) -> None:
        self._reanchor()  # the body rewrapped: zen, contents, a resize or a new document

    def _reanchor(self) -> None:
        """Keep the reading position (a fraction) across a re-layout; record nothing meanwhile."""
        if self.reading_id is None:
            return
        viewer = self.query_one(ReaderPane).viewer
        self._scroll_max = -1.0

        def land() -> None:
            self._scroll_max = viewer.max_scroll_y
            y = round(self._scroll_frac * viewer.max_scroll_y)
            viewer.scroll_to(y=y, animate=False, immediate=True)

        viewer.call_after_refresh(land)

    def _on_reader_scroll(self, y: float) -> None:
        viewer = self.query_one(ReaderPane).viewer
        maximum = viewer.max_scroll_y
        if self.reading_id is None or maximum <= 0:
            return
        if maximum != self._scroll_max:  # a re-layout nobody announced (or one still landing)
            self._reanchor()
            return
        progress = self._scroll_frac = y / maximum
        if progress >= self._saved_progress + 0.05 or progress >= 0.9 > self._saved_progress:
            StateRepo(self.conn).set_progress(self.reading_id, progress, now=self.now())
            self._saved_progress = progress
            self._refresh_row(self.reading_id)  # ◐ becomes ○ at 90 %

    def on_http_wait(self, host: str, seconds: float) -> None:
        # Only while the open item is being fetched: a late callback mustn't overwrite anything.
        if seconds >= 2 and self._extracting is not None and self._extracting == self.reading_id:
            self.query_one(ReaderPane).show_status(
                f"Waiting {seconds:.0f}s for {host} (it asks for a polite crawl delay)…", "dim"
            )

    def watch_mode(self, mode: str) -> None:
        for status in self.query(StatusLine):
            status.mode = mode

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # `run_action` calls this fresh before every dispatch (see `App._check_bindings`),
        # so it needs no cache invalidation of its own -- `refresh_bindings()` elsewhere is
        # only for a Footer-style widget that caches `active_bindings` for display.
        return not (action in _ITEMS_ONLY_ACTIONS and self.mode == "SOURCES")

    def action_help(self) -> None:
        self.push_screen(HelpOverlay())

    def action_cycle_theme(self) -> None:
        names = [name for name in THEMES if name in self.available_themes]
        index = names.index(self.theme) if self.theme in names else -1
        self.theme = names[(index + 1) % len(names)]
        self.refresh_colors()

    def apply_filter(self, f: ItemFilter) -> None:
        self.item_filter = f
        self.query_one(FilterChips).show_filter(f, self.theme)
        self.reload_items()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()
        self.mode = "SEARCH"

    def action_back_to_table(self) -> None:
        if self.reading_id is not None:
            self._refresh_row(self.reading_id)
        self.reading_id = None
        self.screen.remove_class("reading", "zen")
        self.query_one(ItemsTable).focus()
        self.mode = "NORMAL"

    def action_show_items(self) -> None:
        self.query_one(ContentSwitcher).current = "main"
        if self.reading_id is not None:
            # A reading session merely hidden behind Sources, not ended: restore it rather
            # than discarding it through action_back_to_table (which would clear reading_id).
            self.mode = "READ"
            self.query_one(ReaderPane).viewer.document.focus()
        else:
            self.action_back_to_table()
        self.refresh_bindings()

    def action_show_sources(self) -> None:
        self.query_one(ContentSwitcher).current = "sources-view"
        self.refresh_sources()
        self.query_one(SourcesView).table.focus()
        self.mode = "SOURCES"
        self.refresh_bindings()

    def refresh_sources(self) -> None:
        self.query_one(SourcesView).refresh_view()

    def close_reader_if_item_gone(self) -> None:
        """Call after anything that may have deleted the item open in the reader (e.g.
        removing its source cascades its items) -- reading_id must never keep pointing at a
        row that no longer exists."""
        if self.reading_id is not None and ItemsRepo(self.conn).get(self.reading_id) is None:
            self.reading_id = None
            self.screen.remove_class("reading", "zen")

    def action_toggle_zen(self) -> None:
        self.screen.toggle_class("zen")

    def _step(self, delta: int) -> None:
        table = self.query_one(ItemsTable)
        keys = list(table.rows_by_key)
        current = self.reading_id
        here = keys.index(current) if current in table.rows_by_key else table.cursor_row
        there = here + delta
        if 0 <= there < len(keys):  # nothing before the first item or after the last
            table.move_cursor(row=there)
            self.open_item(keys[there])

    def action_next_item(self) -> None:
        self._step(1)

    def action_prev_item(self) -> None:
        self._step(-1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        value = event.value
        self._search_timer = self.set_timer(
            0.15, lambda: self.apply_filter(replace(self.item_filter, search=value))
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.action_back_to_table()

    def action_cycle_sort(self) -> None:
        nxt = SORT_KEYS[(SORT_KEYS.index(self.item_filter.sort) + 1) % len(SORT_KEYS)]
        self.apply_filter(replace(self.item_filter, sort=nxt))

    def action_cycle_show(self) -> None:
        nxt = SHOW_KEYS[(SHOW_KEYS.index(self.item_filter.show) + 1) % len(SHOW_KEYS)]
        self.apply_filter(replace(self.item_filter, show=nxt))

    def action_pick_sources(self) -> None:
        options = [(r.source.id, r.source.id) for r in SourcesRepo(self.conn).list_all()]

        def done(chosen: frozenset[str] | None) -> None:
            if chosen is not None:
                self.apply_filter(replace(self.item_filter, sources=chosen))

        self.push_screen(PickerModal("Sources", options, self.item_filter.sources), done)

    def action_pick_kinds(self) -> None:
        def done(chosen: frozenset[str] | None) -> None:
            if chosen is not None:
                self.apply_filter(replace(self.item_filter, kinds=chosen))

        options = [("Papers", "paper"), ("Articles", "article")]
        self.push_screen(PickerModal("Kind", options, self.item_filter.kinds), done)

    def action_pick_date(self) -> None:
        def done(chosen: str | None) -> None:
            if chosen in DATE_RANGES:
                self.apply_filter(replace(self.item_filter, date=chosen))  # type: ignore[arg-type]

        options = [(DATE_LABELS[d], d) for d in DATE_RANGES]
        self.push_screen(ChoiceModal("Date", options, self.item_filter.date), done)

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        # Only http(s) URLs reach the browser; item URLs come from the DB/feeds, which
        # is untrusted-ish content -- never hand a file:/javascript: scheme to the driver.
        if urlsplit(url).scheme in _OPENABLE_SCHEMES:
            super().open_url(url, new_tab=new_tab)

    def _target(self) -> ItemRow | None:
        """What l/b/x/o act on: the open item while reading, wherever the cursor is."""
        if self.reading_id is not None:
            return get_item_row(self.conn, self.reading_id, now=self.now())
        return self.query_one(ItemsTable).current_row()

    def _toggle(self, field: Toggle) -> None:
        if (row := self._target()) is None:
            return
        StateRepo(self.conn).toggle(row.id, field, now=self.now())
        if self.reading_id is not None:
            self._refresh_row(row.id)  # in place: a re-query would drop the open item from Unread
        else:
            self.reload_items(keep=row.id)

    def action_toggle_like(self) -> None:
        self._toggle("liked")

    def action_toggle_save(self) -> None:
        self._toggle("saved")

    def action_toggle_hide(self) -> None:
        self._toggle("hidden")

    def action_open_browser(self) -> None:
        if (row := self._target()) is None:
            return
        if urlsplit(row.url).scheme not in _OPENABLE_SCHEMES:
            # markup=False: the URL comes from a feed, and notify() parses markup by default.
            self.notify(f"Won't open this link: {row.url}", severity="warning", markup=False)
            return
        self.open_url(row.url)
        StateRepo(self.conn).mark_opened(
            row.id, now=self.now()
        )  # read elsewhere still counts as opened
        if self.reading_id is not None:
            self._refresh_row(row.id)
            return
        rows, _total = list_items(self.conn, replace(self.item_filter, show="all"), now=self.now())
        table = self.query_one(ItemsTable)
        table.show(
            [r for r in rows if r.id in table.rows_by_key], self.now(), self.get_css_variables()
        )
        table.select_key(row.id)
