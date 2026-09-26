import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.app import App
from textual.pilot import Pilot

from augury.core.config import Config, ScoutConfig
from augury.core.db.open import open_db
from augury.tui.app import AuguryApp
from tests.helpers import NullHttp

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
QUIET = Config(scout=ScoutConfig(auto_after_hours=0))  # tests opt in to auto-scout explicitly

RunBefore = Callable[[Pilot], Awaitable[None]]

# Characters that aren't safe in a filename (parametrized test ids can contain
# "/", "[", "]", spaces, quotes, ...).
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


@pytest.fixture(autouse=True)
def utc_timezone(monkeypatch):
    # Snapshots include clock times; pin the zone so they match on every machine.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def instant() -> None:
    await asyncio.sleep(0)


class Gate:
    """A reader debounce that holds until opened, so a test can act inside the window."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    async def __call__(self) -> None:
        await self._event.wait()

    def open(self) -> None:
        self._event.set()

    def close(self) -> None:
        self._event.clear()


async def until(pilot: Pilot, predicate: Callable[[], object], timeout: float = 5.0) -> None:
    """Pause until `predicate()` holds; the timeout only bounds a failure."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"still false after {timeout}s: {predicate}")
        await pilot.pause()


@pytest.fixture
def make_app(paths):
    def factory(
        now: datetime = NOW,
        config: Config | None = None,
        http=None,
        debounce: Callable[[], Awaitable[None]] | None = None,
        editor_runner: Callable[[list[str]], object] | None = None,
    ) -> AuguryApp:
        extra = {"editor_runner": editor_runner} if editor_runner is not None else {}
        app = AuguryApp(
            conn=open_db(paths, now=now),
            config=config or QUIET,
            paths=paths,
            now=lambda: now,
            http_factory=lambda _cfg: http or NullHttp(),
            reader_debounce=debounce or instant,
            **extra,
        )
        app.SEARCH_DEBOUNCE_S = 0  # type: ignore[misc]  # this app only, not the class
        return app

    return factory


def _sanitize(name: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", name)


async def _capture_svg(
    app: App[Any], terminal_size: tuple[int, int], run_before: RunBefore | None
) -> str:
    async with app.run_test(size=terminal_size) as pilot:
        if run_before is not None:
            await run_before(pilot)
        # Let any pending layout/animation settle before the screenshot is taken.
        await pilot.pause()
        return app.export_screenshot()


def _run_capture(
    app: App[Any], terminal_size: tuple[int, int], run_before: RunBefore | None
) -> str:
    """Render `app` and return its SVG screenshot.

    `snap_compare` must be called from a sync test: it drives its own event loop with
    `asyncio.run`, which raises if one is already running on this thread. An earlier
    version worked around that by running the coroutine on a private loop in a spawned
    thread, but that breaks any app holding a thread-affine resource -- in particular
    `AuguryApp.conn` is a `sqlite3.Connection` opened with the default
    `check_same_thread=True`, so using it from another thread raises
    `sqlite3.ProgrammingError`. Every snapshot test in this project is a sync `def`, so
    there's no need to support the async case; fail clearly instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_capture_svg(app, terminal_size, run_before))
    raise RuntimeError("snap_compare must be called from a sync test (def, not async def)")


def compare_svg_to_baseline(
    svg: str, snapshot_path: Path, *, update: bool, actual_dir: Path
) -> bool:
    """The comparison/write logic, factored out of the `snap_compare` fixture so it can
    be exercised directly (see `tests/tui/test_snap_compare.py`) without rendering a
    real Textual app."""
    if update:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(svg, encoding="utf-8")
        return True

    if not snapshot_path.exists():
        raise AssertionError(
            f"No snapshot baseline at {snapshot_path}. Run with --snapshot-update to create it."
        )

    expected = snapshot_path.read_text(encoding="utf-8")
    if svg != expected:
        actual_path = actual_dir / snapshot_path.name
        actual_path.write_text(svg, encoding="utf-8")
        raise AssertionError(
            f"Snapshot mismatch for {snapshot_path.name}.\n"
            f"  baseline: {snapshot_path}\n"
            f"  actual:   {actual_path}"
        )
    return True


@pytest.fixture
def snap_compare(request: pytest.FixtureRequest, tmp_path: Path):
    def compare(
        app: App[Any],
        *,
        terminal_size: tuple[int, int] = (80, 24),
        run_before: RunBefore | None = None,
    ) -> bool:
        svg = _run_capture(app, terminal_size, run_before)

        snapshot_dir = request.path.parent / "__snapshots__" / request.path.stem
        snapshot_path = snapshot_dir / f"{_sanitize(request.node.name)}.svg"
        update = bool(request.config.getoption("--snapshot-update"))

        return compare_svg_to_baseline(svg, snapshot_path, update=update, actual_dir=tmp_path)

    return compare
