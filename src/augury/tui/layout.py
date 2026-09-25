from typing import Literal

Layout = Literal["wide", "medium", "narrow"]

# Spec §8.3: side by side from 100 columns; below that the reader opens full screen.
HIDDEN_COLUMNS: dict[Layout, frozenset[str]] = {
    "wide": frozenset(),
    "medium": frozenset({"min"}),
    "narrow": frozenset({"min", "pop"}),
}


def layout_for(width: int) -> Layout:
    if width >= 160:
        return "wide"
    return "medium" if width >= 100 else "narrow"
