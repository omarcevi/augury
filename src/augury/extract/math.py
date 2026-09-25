from pylatexenc.latex2text import LatexNodes2Text

_CONVERTER = LatexNodes2Text()


def latex_to_unicode(latex: str) -> str:
    """Readable math for a terminal: Unicode when the conversion is clean, raw LaTeX otherwise."""
    latex = latex.strip()
    if not latex:
        return ""
    try:
        text = " ".join(_CONVERTER.latex_to_text(latex).split())
    except Exception:  # pylatexenc raises a variety of parse errors
        return f"${latex}$"
    return text if text and "\\" not in text else f"${latex}$"
