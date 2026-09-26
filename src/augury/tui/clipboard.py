"""Copying text to the system clipboard.

`App.copy_to_clipboard` only writes OSC 52, which iTerm2 (with its clipboard setting on),
Kitty, WezTerm, Ghostty and Alacritty honour but macOS Terminal.app ignores. So on a local
session (not over SSH, where the local tools would fill the *remote* machine's clipboard)
the text is also piped to the first clipboard tool that can work here.
"""

import os
import shutil
import subprocess
from typing import Any
from weakref import WeakKeyDictionary

from textual.app import App
from textual.screen import Screen

TIMEOUT_S = 2.0  # pbcopy & co. answer in milliseconds; never hang the UI on a stuck one

# (argv, the environment variable the tool needs to reach a display, if any). argv lists
# only: nothing here ever goes through a shell.
_TOOLS: tuple[tuple[tuple[str, ...], str | None], ...] = (
    (("pbcopy",), None),
    (("wl-copy",), "WAYLAND_DISPLAY"),
    (("xclip", "-selection", "clipboard"), "DISPLAY"),
    (("xsel", "-b"), "DISPLAY"),
)


def is_remote() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def _local_tool() -> list[str] | None:
    for (name, *args), needs in _TOOLS:
        if needs is not None and not os.environ.get(needs):
            continue
        if path := shutil.which(name):
            return [path, *args]
    return None


def copy(app: App[Any], text: str) -> bool:
    """Put `text` on the clipboard. True if a local tool took it too; False when only OSC 52
    was sent (over SSH, no tool installed, or the tool failed) -- the terminal may still
    have honoured that."""
    app.copy_to_clipboard(text)
    if is_remote() or (argv := _local_tool()) is None:
        return False
    # pbcopy decodes its input with the locale's charset; without a UTF-8 one, "é" breaks.
    env = {**os.environ, "LC_CTYPE": os.environ.get("LC_CTYPE") or "UTF-8"}
    try:
        subprocess.run(
            argv,
            input=text.encode(),
            # Not captured: wl-copy and xclip stay in the background serving the selection,
            # and a captured pipe would keep run() waiting for them until the timeout.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_S,
            check=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        app.log.warning(f"clipboard: {argv[0]} failed: {exc!r}")
        return False
    return True


def copy_and_tell(app: App[Any], text: str, what: str = "") -> bool:
    """Copy, then say what happened in a short toast. "Copied" only when a local tool took the
    text; OSC 52 alone can't be confirmed, so then it says where the text went instead."""
    copied = copy(app, text)
    thing = f" {what}" if what else ""
    if copied:
        message = f"Copied{thing} to clipboard ({len(text)} chars)"
    else:
        message = (
            f"Sent{thing} to the terminal clipboard ({len(text)} chars)"
            " — your terminal must allow clipboard access"
        )
    app.notify(message, markup=False, timeout=2 if copied else 4)
    return copied


# Per screen, the `selections` dict last copied. Textual replaces that dict whenever the
# selection changes, so the same object means the same selection.
_copied_selection: WeakKeyDictionary[Screen[Any], object] = WeakKeyDictionary()


def copy_selection(app: App[Any]) -> None:
    """Copy the screen's text selection, once. TextSelected is posted on every mouse-up, and a
    selection survives e.g. a scrollbar drag: re-copying it would silently replace whatever
    was copied since."""
    screen = app.screen
    selections = screen.selections
    if _copied_selection.get(screen) is selections:
        return
    if text := screen.get_selected_text():
        _copied_selection[screen] = selections
        copy_and_tell(app, text)
