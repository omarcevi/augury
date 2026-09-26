"""No TUI test may reach the real clipboard: tests/tui/conftest.py stubs it for all of them."""

import subprocess

from augury.tui import clipboard
from tests.tui.test_reader import cache, open_first, seed


async def test_a_copy_in_any_tui_test_never_runs_a_clipboard_tool(make_app, monkeypatch):
    ran: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: ran.append(args))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    app = make_app()
    cache(app, seed(app)[0], "# Title\n\nsome text")
    async with app.run_test(size=(130, 40)) as pilot:
        await open_first(pilot)
        await pilot.press("Y")
        assert app.clipboard == "# Title\n\nsome text"  # the app still sees what was copied
    assert ran == []


def test_only_the_clipboards_own_subprocess_is_stubbed():
    # Other code under test (e.g. schedule status) must keep the real subprocess.run.
    assert clipboard.subprocess is not subprocess
    assert clipboard.subprocess.run is not subprocess.run
