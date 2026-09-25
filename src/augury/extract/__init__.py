from augury.core.models import Item
from augury.extract.base import Extracted, UnsupportedItem
from augury.extract.html import extract_article_html
from augury.sources.http import HttpClient


async def extract_item(http: HttpClient, item: Item) -> Extracted:
    if item.kind == "article":
        resp = await http.get(item.url)
        return extract_article_html(resp.content, resp.url)
    raise UnsupportedItem(f"no extractor for {item.kind} items yet")
