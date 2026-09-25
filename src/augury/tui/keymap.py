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
        KeyHint("t", "theme"),
        KeyHint("?", "help"),
        KeyHint("q", "quit"),
    ),
    "SEARCH": (KeyHint("type", "filter"), KeyHint("enter/esc", "back to table")),
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
