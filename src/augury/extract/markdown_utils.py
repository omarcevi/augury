import re

# The alt group allows one level of nested [...] -- LaTeXML emits alt text like
# "[Uncaptioned image]" for uncaptioned figures -- but nothing deeper.
_ALT = r"(?:[^\[\]]|\[[^\[\]]*\])*"
# A "click to enlarge" image link: [![alt](src)](href). Collapsed before the plain-image
# pattern below, since CommonMark doesn't allow a link inside a link -- left alone, the
# outer "](href)" would be stripped to literal trailing text instead of markup.
_LINKED_IMAGE = re.compile(r"\[!\[(" + _ALT + r')\]\(([^)\s]+)(?:\s+"[^"]*")?\)\]\([^)]*\)')
_IMAGE = re.compile(r"!\[(" + _ALT + r')\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")  # noqa: RUF001 -- curly apostrophe, not a lookalike bug
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")
_NOT_PROSE = ("#", ">", "|", "```", "- ", "* ", "[image:", "!")


def _placeholder(alt: str, src: str) -> str:
    # Drop any brackets the alt text carried (e.g. LaTeXML's "[Uncaptioned image]") so the
    # placeholder itself doesn't read as nested markdown.
    text = " ".join(alt.replace("[", "").replace("]", "").split()) or "figure"
    return f"[image: {text}]({src})"


def images_to_placeholders(md: str) -> str:
    # Terminals can't show images; a link keeps them one keypress away.
    md = _LINKED_IMAGE.sub(lambda m: _placeholder(m.group(1), m.group(2)), md)
    return _IMAGE.sub(lambda m: _placeholder(m.group(1), m.group(2)), md)


def tidy(md: str) -> str:
    lines = [line.rstrip() for line in md.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def word_count(md: str) -> int:
    return len(_WORD.findall(_LINK.sub(r"\1", md)))


def lead_paragraph(md: str, *, min_chars: int = 80, max_chars: int = 600) -> str:
    """The article's opening prose, as plain text, for use as a summary."""
    picked: list[str] = []
    for block in re.split(r"\n\s*\n", md):
        block = block.strip()
        if not block or block.startswith(_NOT_PROSE):
            continue
        text = _ESCAPED.sub(r"\1", _LINK.sub(r"\1", block)).replace("**", "")
        text = " ".join(text.split())
        if len(text) < min_chars:
            continue
        picked.append(text)
        if sum(len(p) for p in picked) >= max_chars // 2:
            break
    summary = " ".join(picked)
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1].rsplit(" ", 1)[0] + "…"
