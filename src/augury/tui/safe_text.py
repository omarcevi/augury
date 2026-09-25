"""The only two ways fetched text reaches the screen. Verified on 2026-09-25: a plain str in a
DataTable cell is parsed as markup, and a hostile title became a real clickable link."""

from string.templatelib import Interpolation, Template

from rich.text import Text
from textual.markup import escape

from augury.core.text import strip_control_chars


def text(value: object, style: str = "", *, one_line: bool = False) -> Text:
    return Text(
        strip_control_chars(str(value)),
        style=style,
        no_wrap=one_line,
        overflow="ellipsis" if one_line else "fold",
    )


def markup(template: Template) -> str:
    """Markup with every interpolated value escaped; only the literal parts can style."""
    parts: list[str] = []
    for part in template:
        if isinstance(part, Interpolation):
            value = part.value
            if part.conversion == "r":
                value = repr(value)
            elif part.conversion == "a":
                value = ascii(value)
            rendered = format(value, part.format_spec) if part.format_spec else str(value)
            parts.append(escape(strip_control_chars(rendered)))
        else:
            parts.append(part)
    return "".join(parts)
