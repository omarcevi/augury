from dataclasses import dataclass


@dataclass(frozen=True)
class KeyHint:
    key: str
    label: str


KEYMAP: dict[str, tuple[KeyHint, ...]] = {
    "NORMAL": (
        KeyHint("/", "search"),
        KeyHint("2", "sources"),
        KeyHint("3", "config"),
        KeyHint("S", "sources"),
        KeyHint("K", "kind"),
        KeyHint("D", "date"),
        KeyHint("s", "sort"),
        KeyHint("v", "show"),
        KeyHint("l", "like"),
        KeyHint("b", "save"),
        KeyHint("x", "hide"),
        KeyHint("o", "browser"),
        KeyHint("r", "refresh"),
        KeyHint("t", "theme"),
        KeyHint("g/G", "top/bottom"),
        KeyHint("^d/^u", "half page"),
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
        KeyHint("r", "refresh"),
        KeyHint("l", "like"),
        KeyHint("y", "copy code"),
        KeyHint("Y", "copy article"),
        KeyHint("J/K", "fast scroll"),
        KeyHint("g/G", "top/bottom"),
        KeyHint("^d/^u", "half page"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
        KeyHint("esc", "back"),
    ),
    "SOURCES": (
        KeyHint("+", "add"),
        KeyHint("t", "test fetch"),
        KeyHint("e", "enable/disable"),
        KeyHint("d", "remove"),
        KeyHint("r", "refresh"),
        KeyHint("1", "items"),
        KeyHint("esc", "back"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
    ),
    "CONFIG": (
        KeyHint("1", "items"),
        KeyHint("2", "sources"),
        KeyHint("e", "edit config"),
        KeyHint("esc", "back"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
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
