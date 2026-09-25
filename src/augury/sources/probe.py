from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel

from augury.sources.base import AdapterError
from augury.sources.http import HttpClient, HttpError
from augury.sources.rss import FeedInfo, inspect_feed

COMMON_FEED_PATHS = (
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
    "/blog/feed",
    "/blog/rss.xml",
)
PREFIX_FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
FEED_TYPES = {"application/rss+xml", "application/atom+xml"}
FEED_CONTENT_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
FEED_ROOT_TAGS = (b"<rss", b"<feed", b"<rdf:rdf")
MIN_ENTRIES = 3
MAX_AGE = timedelta(days=365)
MAX_ALTERNATE_LINKS = 5
_SNIFF_BYTES = 4096
# Longest first: UTF-32 BOMs share a prefix with the UTF-16 ones.
_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


class ProbeAttempt(BaseModel):
    url: str
    outcome: str


class ProbeResult(BaseModel):
    input_url: str
    candidates: list[FeedInfo]
    sitemaps: list[str]
    attempts: list[ProbeAttempt]


def feed_links(html: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for tag in soup.find_all("link", attrs={"rel": "alternate"}):
        if not isinstance(tag, Tag):
            continue
        href = tag.get("href")
        if str(tag.get("type") or "").lower() in FEED_TYPES and isinstance(href, str):
            links.append(urljoin(base_url, href))
    return list(dict.fromkeys(links))


def candidate_paths(page_url: str) -> list[str]:
    parts = urlsplit(page_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    urls = [origin + path for path in COMMON_FEED_PATHS]
    if prefix := parts.path.rstrip("/"):
        urls += [origin + prefix + path for path in PREFIX_FEED_PATHS]
    return list(dict.fromkeys(urls))


def _looks_like_feed(content: bytes, content_type: str = "") -> bool:
    """A pasted URL is a feed if its Content-Type says so, or its body starts with a feed
    root element once a BOM, whitespace, the XML prolog and any comments are skipped."""
    if content_type.split(";")[0].strip().lower() in FEED_CONTENT_TYPES:
        return True
    head = content[:_SNIFF_BYTES]
    for bom in _BOMS:
        if head.startswith(bom):
            head = head[len(bom) :]
            break
    head = head.lstrip()
    for _ in range(10):  # a handful of comments/prolog at most; never scan the whole body
        lower = head.lower()
        if lower.startswith(b"<?xml") and (end := head.find(b"?>")) != -1:
            head = head[end + 2 :].lstrip()
        elif lower.startswith(b"<!--") and (end := head.find(b"-->")) != -1:
            head = head[end + 3 :].lstrip()
        else:
            break
    return head.lower().startswith(FEED_ROOT_TAGS)


def _rejection(info: FeedInfo, now: datetime) -> str | None:
    if info.entries < MIN_ENTRIES:
        return f"only {info.entries} entries (need {MIN_ENTRIES})"
    if info.newest and now - info.newest > MAX_AGE:
        return f"newest entry is from {info.newest:%Y-%m-%d} (older than a year)"
    return None


async def probe_url(url: str, http: HttpClient, *, now: datetime) -> ProbeResult:
    """Find valid feeds for a page with plain code: no LLM, at most a dozen requests."""
    attempts: list[ProbeAttempt] = []
    candidates: list[FeedInfo] = []
    tried: set[str] = set()

    async def try_feed(feed_url: str) -> None:
        if feed_url in tried:
            return
        tried.add(feed_url)
        try:
            info = await inspect_feed(feed_url, http)
        except (HttpError, AdapterError) as e:
            attempts.append(ProbeAttempt(url=feed_url, outcome=str(e)))
            return
        if reason := _rejection(info, now):
            attempts.append(ProbeAttempt(url=feed_url, outcome=reason))
        elif info.feed_url not in {c.feed_url for c in candidates}:
            attempts.append(ProbeAttempt(url=feed_url, outcome="valid feed"))
            candidates.append(info)

    page = await http.get(url)
    if _looks_like_feed(page.content, page.headers.get("content-type", "")):
        await try_feed(page.url)
    else:
        for link in feed_links(page.content, page.url)[:MAX_ALTERNATE_LINKS]:
            await try_feed(link)
        if not candidates:
            for guess in candidate_paths(page.url):
                await try_feed(guess)
                if candidates:
                    break
    return ProbeResult(
        input_url=url,
        candidates=candidates,
        sitemaps=await http.sitemaps(page.url),
        attempts=attempts,
    )
