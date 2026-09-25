from dataclasses import dataclass


@dataclass(frozen=True)
class KeyHint:
    key: str
    label: str


KEYMAP: dict[str, tuple[KeyHint, ...]] = {
    "NORMAL": (
        KeyHint("/", "search"),
        KeyHint("S", "sources"),
        KeyHint("K", "kind"),
        KeyHint("D", "date"),
        KeyHint("s", "sort"),
        KeyHint("v", "show"),
        KeyHint("l", "like"),
        KeyHint("b", "save"),
        KeyHint("x", "hide"),
        KeyHint("o", "browser"),
        KeyHint("t", "theme"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
    ),
    "SEARCH": (KeyHint("type", "filter"), KeyHint("enter/esc", "back to table")),
    "READ": (
        KeyHint("j/k", "scroll"),
        KeyHint("[ ]", "section"),
        KeyHint("c", "contents"),
        KeyHint("z", "zen"),
        KeyHint("n/p", "next/prev"),
        KeyHint("o", "browser"),
        KeyHint("l", "like"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
        KeyHint("esc", "back"),
    ),
}

THEMES = (
    "textual-dark",
    "dracula",
    "nord",
    "gruvbox",
    "tokyo-night",
    "catppuccin-mocha",
    "textual-light",
)
