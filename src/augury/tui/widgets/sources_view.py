from typing import TYPE_CHECKING, ClassVar, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, OptionList

from augury.core.db.sources_repo import BuiltinSourceError, SourceExists, SourcesRepo
from augury.core.models import FetchState
from augury.sources.probe import build_feed_source, page_url, probe_url
from augury.sources.registry import adapter_for
from augury.tui.widgets.add_source_panel import AddSourcePanel
from augury.tui.widgets.confirm_modal import ConfirmModal
from augury.tui.widgets.source_detail import SourceDetail
from augury.tui.widgets.sources_table import SourcesTable

if TYPE_CHECKING:
    from augury.tui.app import AuguryApp


class SourcesView(Horizontal):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("plus", "add", "add"),
        Binding("t", "test_fetch", "test fetch"),  # overrides the app's theme key while here
        Binding("e", "toggle_enabled", "enable/disable"),
        Binding("d", "remove", "remove"),
        Binding("escape", "back", "back"),
    ]

    def compose(self) -> ComposeResult:
        yield SourcesTable(id="sources-table")
        with Vertical(id="sources-side"):
            yield SourceDetail(id="source-detail")
            yield AddSourcePanel(id="add-source")

    @property
    def table(self) -> SourcesTable:
        return self.query_one(SourcesTable)

    @property
    def augury_app(self) -> AuguryApp:
        # `self.app` is typed as a plain `App`; this keeps the AuguryApp-specific
        # attributes (`conn`, `http`, `now`, `reload_items`, ...) usable without
        # sprinkling `# type: ignore` everywhere below.
        return cast("AuguryApp", self.app)

    def refresh_view(self) -> None:
        app = self.augury_app
        repo = SourcesRepo(app.conn)
        keep = self.table.current()
        self.table.show(repo.list_all(), repo.item_counts(), app.now(), app.get_css_variables())
        if keep is not None:
            self.table.select_key(keep.source.id)
        self._show_current()

    def _show_current(self, **extra: object) -> None:
        record = self.table.current()
        counts = SourcesRepo(self.augury_app.conn).item_counts()
        count = counts.get(record.source.id, 0) if record else 0
        self.query_one(SourceDetail).show(record, count, **extra)  # type: ignore[arg-type]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.table:
            self._show_current()

    def action_toggle_enabled(self) -> None:
        if (record := self.table.current()) is not None:
            repo = SourcesRepo(self.augury_app.conn)
            repo.set_enabled(record.source.id, not record.source.enabled)
            self.refresh_view()

    def action_remove(self) -> None:
        record = self.table.current()
        if record is None:
            return
        if record.source.origin == "builtin":
            self.augury_app.notify(
                f"{record.source.id} is built in: press e to disable it instead.",
                severity="warning",
                markup=False,
            )
            return

        def done(yes: bool | None) -> None:
            if yes:
                try:
                    SourcesRepo(self.augury_app.conn).remove(record.source.id)
                except BuiltinSourceError:
                    return
                # The removed source's items cascade-delete; the reader must never keep
                # pointing at one that's now gone.
                self.augury_app.close_reader_if_item_gone()
                self.refresh_view()
                self.augury_app.reload_items()

        message = f"Remove {record.source.id} and all its items?"
        self.augury_app.push_screen(ConfirmModal(message), done)

    @work(exclusive=True, group="sources")
    async def action_test_fetch(self) -> None:
        record = self.table.current()
        app = self.augury_app
        if record is None or app.http is None:
            return
        try:
            result = await adapter_for(record.source).fetch(record.source, FetchState(), app.http)
        except Exception as exc:  # show any failure in the detail pane
            self._show_current(error=f"{type(exc).__name__}: {exc}")
            return
        self._show_current(samples=[item.title for item in result.items])

    def action_add(self) -> None:
        self.query_one(SourceDetail).display = False
        panel = self.query_one(AddSourcePanel)
        panel.display = True
        panel.reset()

    def action_back(self) -> None:
        if self.query_one(AddSourcePanel).display:
            self.action_close_add()
        else:
            self.augury_app.action_show_items()

    def action_close_add(self) -> None:
        self.query_one(AddSourcePanel).display = False
        self.query_one(SourceDetail).display = True
        self.table.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "add-url" and event.value.strip():
            event.stop()
            self._probe(event.value.strip())

    @work(exclusive=True, group="sources")
    async def _probe(self, url: str) -> None:
        panel = self.query_one(AddSourcePanel)
        try:
            url = page_url(url)
        except ValueError as exc:
            panel.show_status(str(exc))
            return
        panel.show_status("Looking for a feed…")
        app = self.augury_app
        if app.http is None:
            return
        try:
            result = await probe_url(url, app.http, now=app.now())
        except Exception as exc:  # unreachable page, robots block, …
            panel.show_status(f"Couldn't read that page: {exc}")
            return
        panel.show_result(result)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        panel = self.query_one(AddSourcePanel)
        if event.option_list.id != "add-candidates" or event.option.id is None:
            return
        info = panel.candidates[int(event.option.id)]
        app = self.augury_app
        repo = SourcesRepo(app.conn)
        if existing := repo.find_by_feed_url(info.feed_url):
            panel.show_status(f"That feed is already added as {existing}.")
            return
        source = build_feed_source(repo, info, added_via="url_probe")
        try:
            repo.add(source, now=app.now())
        except SourceExists as exc:
            app.notify(str(exc), severity="warning", markup=False)
            return
        app.notify(f"Added {source.id}. It will be fetched on the next scout.", markup=False)
        self.action_close_add()
        self.refresh_view()
