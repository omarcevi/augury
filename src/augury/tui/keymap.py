from dataclasses import dataclass


@dataclass(frozen=True)
class KeyHint:
    key: str
    label: str


KEYMAP: dict[str, tuple[KeyHint, ...]] = {
    "NORMAL": (KeyHint("?", "help"), KeyHint("t", "theme"), KeyHint("q", "quit")),
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
