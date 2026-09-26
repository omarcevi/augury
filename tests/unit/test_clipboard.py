"""P9: the clipboard helper. The real clipboard is never touched: subprocess.run,
shutil.which and App.copy_to_clipboard are all patched."""

import subprocess
from typing import Any

import pytest
from textual.app import App

from augury.tui import clipboard


class Recorder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, 0)


@pytest.fixture
def osc52(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(App, "copy_to_clipboard", lambda self, text: sent.append(text))
    return sent


@pytest.fixture
def run(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(clipboard.subprocess, "run", recorder)
    return recorder


@pytest.fixture
def local(monkeypatch):
    """A local (not SSH) session with only the tools named in `installed` on PATH."""

    def setup(*installed: str, **env: str) -> None:
        for name in ("SSH_CONNECTION", "SSH_TTY", "WAYLAND_DISPLAY", "DISPLAY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            clipboard.shutil,
            "which",
            lambda tool: f"/usr/bin/{tool}" if tool in installed else None,
        )

    return setup


def test_copy_sends_osc52_and_pipes_to_pbcopy(osc52, run, local):
    local("pbcopy", "xclip")
    assert clipboard.copy(App(), "héllo ✓") is True
    assert osc52 == ["héllo ✓"]
    [(argv, kwargs)] = run.calls
    assert argv == ["/usr/bin/pbcopy"]
    assert kwargs["input"] == "héllo ✓".encode()
    assert 0 < kwargs["timeout"] <= 5
    assert not kwargs.get("shell")


@pytest.mark.parametrize(
    ("installed", "env", "argv"),
    [
        (("wl-copy", "xclip"), {"WAYLAND_DISPLAY": "wayland-0"}, ["/usr/bin/wl-copy"]),
        (
            ("wl-copy", "xclip"),
            {"DISPLAY": ":0"},  # X11: wl-copy is installed but can't work
            ["/usr/bin/xclip", "-selection", "clipboard"],
        ),
        (("xsel",), {"DISPLAY": ":0"}, ["/usr/bin/xsel", "-b"]),
    ],
)
def test_linux_uses_the_first_tool_that_can_work(osc52, run, local, installed, env, argv):
    local(*installed, **env)
    assert clipboard.copy(App(), "x") is True
    assert [call[0] for call in run.calls] == [argv]


@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_TTY"])
def test_over_ssh_only_osc52_is_used(osc52, run, local, monkeypatch, variable):
    local("pbcopy")
    monkeypatch.setenv(variable, "10.0.0.1 5000 10.0.0.2 22")
    assert clipboard.copy(App(), "remote") is False
    assert osc52 == ["remote"] and run.calls == []


def test_no_local_tool_still_sends_osc52(osc52, run, local):
    local()
    assert clipboard.copy(App(), "x") is False
    assert osc52 == ["x"] and run.calls == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["pbcopy"]),
        subprocess.TimeoutExpired(["pbcopy"], 2),
        FileNotFoundError("pbcopy"),
        PermissionError("pbcopy"),
    ],
)
def test_a_failing_fallback_never_raises(osc52, monkeypatch, local, error):
    local("pbcopy")
    monkeypatch.setattr(clipboard.subprocess, "run", Recorder(error))
    assert clipboard.copy(App(), "x") is False
    assert osc52 == ["x"]


SENT = "your terminal must allow clipboard access"


class Toasts(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.toasts: list[tuple[str, bool]] = []

    def notify(self, message, *, markup=True, **kwargs) -> None:  # type: ignore[override]
        self.toasts.append((message, markup))


def test_the_toast_says_copied_only_when_a_local_tool_took_it(osc52, run, local):
    local("pbcopy")
    app = Toasts()
    clipboard.copy_and_tell(app, "héllo", "code block")
    clipboard.copy_and_tell(app, "abc")
    assert app.toasts == [
        ("Copied code block to clipboard (5 chars)", False),
        ("Copied to clipboard (3 chars)", False),
    ]


@pytest.mark.parametrize("how", ["ssh", "no tool", "tool failed"])
def test_otherwise_the_toast_says_it_went_to_the_terminal(osc52, monkeypatch, local, how):
    if how == "no tool":
        local()
    else:
        local("pbcopy")
    if how == "ssh":
        monkeypatch.setenv("SSH_TTY", "/dev/ttys001")
    monkeypatch.setattr(
        clipboard.subprocess,
        "run",
        Recorder(subprocess.CalledProcessError(1, ["pbcopy"]) if how == "tool failed" else None),
    )
    app = Toasts()
    clipboard.copy_and_tell(app, "abc", "article")
    clipboard.copy_and_tell(app, "abcd")
    assert app.toasts == [
        (f"Sent article to the terminal clipboard (3 chars) — {SENT}", False),
        (f"Sent to the terminal clipboard (4 chars) — {SENT}", False),
    ]
