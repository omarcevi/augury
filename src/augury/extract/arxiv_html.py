from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from augury.extract.base import MIN_WORDS, ExtractionError
from augury.extract.markdown_utils import images_to_placeholders, tidy, word_count
from augury.extract.math import latex_to_unicode

_CHROME = (
    "script",
    "style",
    "nav",
    "footer",
    "button",
    ".ltx_page_footer",
    ".ltx_role_navigation",
    ".ltx_authors",
    ".ltx_dates",
)


def arxiv_html_to_markdown(html: bytes, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article.ltx_document")
    if article is None:
        raise ExtractionError("no arXiv HTML article on the page")
    base_tag = soup.find("base")
    base_href = base_tag.get("href") if isinstance(base_tag, Tag) else None
    # No <base> tag: resolve relative URLs against the document's own URL, exactly like a
    # browser would (urljoin already implements that -- forcing a trailing slash would treat
    # the id itself as a directory and double it up, e.g. .../2609.24984/2609.24984v1/x.png).
    base = urljoin(base_url, base_href) if isinstance(base_href, str) else base_url
    for selector in _CHROME:
        for node in article.select(selector):
            node.decompose()
    # Display equations live in one-cell tables; flatten them to a paragraph before markdownify.
    for table in article.select("table.ltx_equation, table.ltx_equationgroup"):
        formulas = [
            latex_to_unicode(str(m.get("alttext") or m.get_text(" ")))
            for m in table.find_all("math")
        ]
        paragraph = soup.new_tag("p")
        paragraph.string = "    ".join(f for f in formulas if f)
        table.replace_with(paragraph)
    for math in article.find_all("math"):
        if isinstance(math, Tag):
            math.replace_with(latex_to_unicode(str(math.get("alttext") or math.get_text(" "))))
    for img in article.find_all("img"):
        if isinstance(img, Tag) and isinstance(src := img.get("src"), str):
            img["src"] = urljoin(base, src)
    md = tidy(images_to_placeholders(markdownify(str(article), heading_style="ATX", bullets="-")))
    words = word_count(md)
    if words < MIN_WORDS:
        raise ExtractionError(f"too little text extracted ({words} words)")
    return md
