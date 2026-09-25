import re
from pathlib import Path

import pytest

from augury.extract import math as math_mod
from augury.extract.arxiv_html import arxiv_html_to_markdown
from augury.extract.base import ExtractionError
from augury.extract.pdf import pdf_to_markdown
from tests.pdf_builder import make_pdf

PAGES = Path(__file__).parents[1] / "fixtures" / "pages"
# Split across adjacent literals (byte-identical once concatenated) so no line exceeds ruff's
# 100-char limit. The trailing paragraph is filler, only so the total clears MIN_WORDS and
# this fixture exercises the success path rather than the thin-content fallback covered by
# test_thin_arxiv_html_is_an_error / test_thin_arxiv_html_falls_back_to_pdf below.
MINI = (
    b"""<html><head><base href="/html/2609.00001v1/"></head><body>
<nav>arXiv navigation</nav>
<article class="ltx_document"><h1 class="ltx_title">A Tiny Paper</h1>
<section class="ltx_section"><h2 class="ltx_title ltx_title_section">1 Introduction</h2>
<p>We bound <math alttext="\\alpha\\leq 1" display="inline"><mi>a</mi></math> for all inputs.</p>
<table class="ltx_equation"><tr><td><math alttext="x^{2}+\\mathbb{R}" display="block">"""
    b"""<mi>x</mi></math></td></tr></table>
<img src="x1.png" alt="Overview figure">"""
    b"""<p>The rest of this paragraph is filler text added only so the fixture clears the
extractor minimum word threshold, exercising the ordinary success path rather than the
thin content fallback that a separate, deliberately short stub tests elsewhere.</p>
</section></article></body></html>"""
)

# Below MIN_WORDS on purpose: exercises the thin-content guard and its PDF fallback.
THIN_STUB = (
    b'<html><body><article class="ltx_document">'
    b'<h1 class="ltx_title">Stub</h1><p>Too short.</p></article></body></html>'
)


def test_latex_to_unicode():
    # Greek alpha and blackboard R below are the expected Unicode math output, not lookalikes.
    assert math_mod.latex_to_unicode(r"\alpha \leq x^{2} + \mathbb{R}") == "α≤x^2 + ℝ"  # noqa: RUF001
    assert math_mod.latex_to_unicode("") == ""


def test_unconvertible_latex_falls_back_to_raw(monkeypatch):
    def boom(_latex: str) -> str:
        raise ValueError("parse error")

    monkeypatch.setattr(math_mod._CONVERTER, "latex_to_text", boom)
    assert math_mod.latex_to_unicode(r"\weird{x}") == r"$\weird{x}$"


def test_mini_arxiv_page_becomes_markdown():
    md = arxiv_html_to_markdown(MINI, "https://arxiv.org/html/2609.00001v1")
    assert "## 1 Introduction" in md and "α≤1" in md and "ℝ" in md  # noqa: RUF001
    assert "<math" not in md and "arXiv navigation" not in md
    assert "[image: Overview figure](https://arxiv.org/html/2609.00001v1/x1.png)" in md


def test_real_arxiv_page_keeps_sections_and_math():
    md = arxiv_html_to_markdown(
        (PAGES / "arxiv_2609.24984.html").read_bytes(), "https://arxiv.org/html/2609.24984"
    )
    assert re.search(r"^## .*Introduction", md, re.M)
    assert "ℝ" in md and "<math" not in md  # noqa: RUF001


def test_real_arxiv_page_converts_every_image_to_a_placeholder():
    # The real teaser figure has LaTeXML's bracketed "[Uncaptioned image]" alt text; the
    # other three figures have plain alt text. All four must become placeholders.
    md = arxiv_html_to_markdown(
        (PAGES / "arxiv_2609.24984.html").read_bytes(), "https://arxiv.org/html/2609.24984"
    )
    assert "![" not in md
    assert md.count("[image: ") == 4
    assert "[image: Uncaptioned image]" in md


def test_page_without_an_article_is_an_error():
    with pytest.raises(ExtractionError):
        arxiv_html_to_markdown(
            b"<html><body>No HTML version</body></html>", "https://arxiv.org/html/x"
        )


def test_thin_arxiv_html_is_an_error():
    with pytest.raises(ExtractionError, match="too little text"):
        arxiv_html_to_markdown(THIN_STUB, "https://arxiv.org/html/2609.00002")


def test_pdf_text_is_extracted_with_a_notice():
    words = "Speculative decoding verifies draft tokens in parallel to cut latency."
    md = pdf_to_markdown(make_pdf([words] * 8))
    assert md.startswith("> Extracted from the PDF") and "draft tokens" in md


def test_pdf_without_text_fails_clearly():
    with pytest.raises(ExtractionError, match="no extractable text"):
        pdf_to_markdown(make_pdf([]))
