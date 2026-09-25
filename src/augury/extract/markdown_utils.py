import re

# A "click to enlarge" image link: [![alt](src)](href). Collapsed before the plain-image
# pattern below, since CommonMark doesn't allow a link inside a link -- left alone, the
# outer "](href)" would be stripped to literal trailing text instead of markup.
_LINKED_IMAGE = re.compile(r'\[!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)\]\([^)]*\)')
_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")  # noqa: RUF001 -- curly apostrophe, not a lookalike bug


def _placeholder(alt: str, src: str) -> str:
    text = " ".join(alt.split()) or "figure"
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
