import trafilatura
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from augury.extract.base import MIN_WORDS, Extracted, ExtractionError
from augury.extract.markdown_utils import images_to_placeholders, tidy, word_count
from augury.extract.profiles import SiteProfile, profile_for


def _with_profile(html: bytes, profile: SiteProfile) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(profile.container)
    if root is None:
        return None
    for selector in profile.remove:
        for node in root.select(selector):
            node.decompose()
    title = root.find("h1")
    if profile.drop_block_after_title_containing and isinstance(title, Tag):
        block = title.find_next_sibling()
        marker = profile.drop_block_after_title_containing
        if isinstance(block, Tag) and marker in block.get_text(" "):
            block.decompose()
    for anchor in root.select("h1 a, h2 a, h3 a, h4 a"):
        if not anchor.get_text(strip=True):
            anchor.decompose()
    return tidy(images_to_placeholders(markdownify(str(root), heading_style="ATX", bullets="-")))


def extract_article_html(html: bytes, url: str) -> Extracted:
    if (profile := profile_for(url)) is not None:
        md = _with_profile(html, profile)
        if md and word_count(md) >= MIN_WORDS:
            return Extracted(md, f"profile:{profile.name}", word_count(md))
        # The container wasn't there (a redesign?); fall through to the generic extractor.
    md = (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            include_images=True,
        )
        or ""
    )
    md = tidy(images_to_placeholders(md))
    words = word_count(md)
    if words < MIN_WORDS:
        raise ExtractionError(f"too little text extracted ({words} words)")
    return Extracted(md, "trafilatura", words)
