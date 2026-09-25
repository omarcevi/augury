from augury.core.models import Item
from augury.extract.arxiv_html import arxiv_html_to_markdown
from augury.extract.base import Extracted, ExtractionError, UnsupportedItem
from augury.extract.html import extract_article_html
from augury.extract.markdown_utils import word_count
from augury.extract.pdf import pdf_to_markdown
from augury.sources.http import HttpClient, HttpError

ARXIV = "https://arxiv.org"


async def extract_item(http: HttpClient, item: Item) -> Extracted:
    if item.kind == "article":
        resp = await http.get(item.url)
        return extract_article_html(resp.content, resp.url)
    if not item.arxiv_id:
        raise UnsupportedItem("this paper has no arXiv id")
    try:
        resp = await http.get(f"{ARXIV}/html/{item.arxiv_id}")
        md = arxiv_html_to_markdown(resp.content, resp.url)
        return Extracted(md, "arxiv_html", word_count(md))
    except HttpError as e:
        if e.status != 404:
            raise
    except ExtractionError:
        pass
    # No HTML rendering for this paper: fall back to the PDF (arXiv's 15 s crawl delay applies).
    resp = await http.get(f"{ARXIV}/pdf/{item.arxiv_id}")
    md = pdf_to_markdown(resp.content)
    return Extracted(md, "pdf", word_count(md))
