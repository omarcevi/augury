"""The discovery agent's function tools (spec §5.6): fetch_page, probe_feeds, probe_sitemap and
test_recipe. Every page-derived string reaches the model inside a fenced data block (§5.8);
only our own values (ok, errors, counts, recipe hashes) sit outside it. test_recipe runs the
real adapter and records each recipe that worked in VerifiedRecipes, the only recipes
submit_candidates will accept. Every fetch, and every hop of it, reaches public addresses only
(sources/public_only.py): the URLs come from a model and from the pages it reads."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag
from pydantic import ValidationError

from augury.agents.discovery.models import (
    MAX_URL_CHARS,
    SAMPLES,
    RecipeArg,
    SampleItem,
    UserRecipe,
    recipe_hash,
)
from augury.agents.discovery.search import fence
from augury.core.models import FetchState, SitemapRecipe, Source
from augury.core.text import strip_control_chars
from augury.sources.base import AdapterError
from augury.sources.http import HttpClient, HttpError
from augury.sources.probe import feed_links, page_url, probe_url
from augury.sources.public_only import PublicOnlyHttp, Resolve
from augury.sources.registry import ADAPTERS
from augury.sources.sitemap import newest_first, parse_sitemap

TEXT_CHARS = 4000  # page text + outline stay within the spec's 8,000 characters
OUTLINE_CHARS = 4000
MAX_LINKS = 40
MAX_SITEMAPS = 3
SAMPLE_PATHS = 15
STALE_AFTER = timedelta(days=365)  # the probe's validity rule, applied to tested recipes too
_KEEP_ATTRS = ("class", "id", "href", "datetime")
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template", "head")


def _error(exc: BaseException) -> str:
    return " ".join(strip_control_chars(f"{type(exc).__name__}: {exc}").split())[:300]


def _fits(url: str) -> bool:
    return len(url) <= MAX_URL_CHARS


def _short_urls(urls: list[str], limit: int) -> list[str]:
    return [u for u in urls if _fits(u)][:limit]


def _page(url: str) -> str:
    return page_url(url)  # adds https:// to a bare domain; refuses anything but http(s)


def _outline(soup: BeautifulSoup) -> str:
    """The page's markup without scripts, styles or most attributes: enough structure (tags,
    classes, hrefs) for choosing html_listing selectors."""
    root = soup.find("main") or soup.body or soup
    if not isinstance(root, Tag):
        return ""
    for node in root.find_all(_DROP_TAGS):
        node.decompose()
    for node in root.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    for node in root.find_all(True):
        node.attrs = {k: v for k, v in node.attrs.items() if k in _KEEP_ATTRS}
    return re.sub(r"\s+", " ", str(root))[:OUTLINE_CHARS]


def page_info(html: str, content: bytes, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if (
            isinstance(key, str)
            and isinstance(value, str)
            and key.lower()
            in (
                "description",
                "og:title",
                "og:description",
                "og:site_name",
                "og:type",
                "generator",
            )
        ):
            meta[key.lower()] = value[:300]
    host = urlsplit(url).hostname
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    areas = [a for a in soup.find_all(["nav", "header", "main", "article"]) if isinstance(a, Tag)]
    anchors = (a for area in areas or [soup] for a in area.find_all("a", href=True))
    for a in anchors:
        if len(links) >= MAX_LINKS:  # across every area, not per area
            break
        if not isinstance(a, Tag) or not isinstance(href := a.get("href"), str):
            continue
        target = urljoin(url, href)
        if urlsplit(target).scheme not in ("http", "https") or target in seen or not _fits(target):
            continue
        seen.add(target)
        same_site = urlsplit(target).hostname == host
        links.append({"text": a.get_text(" ", strip=True)[:80], "url": target})
        if not same_site:
            links[-1]["external"] = "yes"
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = " ".join(soup.get_text(" ").split())[:TEXT_CHARS]
    return {  # links last: if the result is cut to fit, the text and outline survive
        "title": title[:200],
        "meta": meta,
        "feed_links": _short_urls(feed_links(content, url), 5),
        "text": text,
        "outline": _outline(soup),
        "links": links,
    }


@dataclass
class VerifiedRecipe:
    recipe: UserRecipe
    samples: list[SampleItem]


@dataclass
class VerifiedRecipes:
    """Recipes test_recipe ran successfully in this discovery run, by recipe hash."""

    by_hash: dict[str, VerifiedRecipe] = field(default_factory=dict)

    def add(self, recipe: UserRecipe, samples: list[SampleItem]) -> str:
        h = recipe_hash(recipe)
        self.by_hash[h] = VerifiedRecipe(recipe, samples)
        return h

    def get(self, h: str) -> VerifiedRecipe | None:
        return self.by_hash.get(h)


def _sample(item: Any) -> SampleItem:  # the item's URL fits MAX_URL_CHARS
    published = item.published_at.isoformat() if item.published_at else None
    return SampleItem(title=item.title[:200], url=item.url, published_at=published)


class DiscoveryTools:
    """The function tools, bound to one run's HTTP client, clock and tested-recipe registry.
    `http` is wrapped so that it only reaches public addresses; `resolve` looks host names up
    for that check (the system resolver by default; tests pass a fake)."""

    def __init__(
        self,
        http: HttpClient,
        *,
        now: Callable[[], datetime],
        tested: VerifiedRecipes,
        resolve: Resolve | None = None,
    ) -> None:
        self.public = PublicOnlyHttp(http, resolve=resolve)
        self.http: HttpClient = self.public
        self.now, self.tested = now, tested

    def functions(self) -> list[Callable[..., Any]]:
        return [self.fetch_page, self.probe_feeds, self.probe_sitemap, self.test_recipe]

    async def fetch_page(self, url: str) -> dict[str, Any]:
        """Read one web page: its title, meta tags, feed links, navigation and article links,
        the start of its text, and an outline of its HTML (for choosing CSS selectors)."""
        try:
            resp = await self.http.get(_page(url))
        except (ValueError, HttpError) as e:
            return {"ok": False, "error": _error(e)}
        return {"ok": True, "page": fence(page_info(resp.text(), resp.content, resp.url))}

    async def probe_feeds(self, url: str) -> dict[str, Any]:
        """Look for RSS/Atom feeds for a site or blog page: <link rel=alternate> tags and the
        usual feed paths. Lists the valid feeds (3+ entries, updated within a year)."""
        try:
            result = await probe_url(_page(url), self.http, now=self.now())
        except (ValueError, HttpError) as e:
            return {"ok": False, "error": _error(e)}
        feeds = [
            {
                "feed_url": c.feed_url,
                "title": c.title[:200],
                "homepage": c.homepage if _fits(c.homepage) else "",
                "entries": c.entries,
                "newest": c.newest.isoformat() if c.newest else None,
                "sample_titles": [t[:200] for t in c.sample_titles[:SAMPLES]],
            }
            for c in result.candidates
            if _fits(c.feed_url)
        ]
        tried = [
            {"url": a.url, "outcome": a.outcome[: MAX_URL_CHARS + 100]}
            for a in result.attempts
            if _fits(a.url)
        ][:12]
        sitemaps = _short_urls(result.sitemaps, 5)
        return {
            "ok": True,
            "valid_feeds": len(feeds),
            "data": fence({"feeds": feeds, "tried": tried, "sitemaps": sitemaps}),
        }

    async def probe_sitemap(self, url: str) -> dict[str, Any]:
        """Find a site's sitemaps (robots.txt Sitemap: lines, /sitemap.xml, /sitemap_index.xml)
        and show what each lists: child sitemaps, or a count and the newest URL paths (to
        choose an include_pattern from)."""
        try:
            page = _page(url)
            await self.public.check(page)
        except (ValueError, HttpError) as e:
            return {"ok": False, "error": _error(e)}
        parts = urlsplit(page)
        origin = f"{parts.scheme}://{parts.netloc}"
        try:
            listed = _short_urls(await self.http.sitemaps(page), MAX_SITEMAPS)
        except HttpError:
            listed = []
        found: list[dict[str, Any]] = []
        tried: list[dict[str, str]] = []
        for sitemap_url in list(
            dict.fromkeys([*listed, f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"])
        )[:MAX_SITEMAPS]:
            try:
                resp = await self.http.get(sitemap_url)
                parsed = parse_sitemap(resp.content, resp.url)
            except (HttpError, AdapterError) as e:
                tried.append({"url": sitemap_url, "outcome": _error(e)})
                continue
            if parsed.children:
                children = [
                    {"url": c.url, "lastmod": c.lastmod.isoformat() if c.lastmod else None}
                    for c in newest_first(parsed.children)
                    if _fits(c.url)
                ][:SAMPLE_PATHS]
                found.append({"sitemap_url": resp.url, "kind": "index", "children": children})
            else:
                newest = newest_first(parsed.urls)
                found.append(
                    {
                        "sitemap_url": resp.url,
                        "kind": "urlset",
                        "urls": len(parsed.urls),
                        "newest_paths": _short_urls(
                            [urlsplit(e.url).path for e in newest], SAMPLE_PATHS
                        ),
                    }
                )
        return {"ok": True, "sitemaps": len(found), "data": fence({"found": found, "tried": tried})}

    async def test_recipe(self, recipe: RecipeArg) -> dict[str, Any]:
        """Run a recipe with the real adapter, as the daily scout would. Returns up to 3 sample
        items and the recipe_hash to cite in submit_candidates, or the error to fix."""
        try:
            arg = recipe if isinstance(recipe, RecipeArg) else RecipeArg.model_validate(recipe)
            parsed = arg.to_recipe()
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'][1:]) or 'recipe'}: {err['msg']}"
                for err in e.errors()
            )
            return {"ok": False, "error": f"invalid recipe: {problems}"[:300]}
        run = parsed
        if isinstance(parsed, SitemapRecipe):  # a test reads 3 pages, not max_new_per_run
            run = parsed.model_copy(update={"max_new_per_run": SAMPLES})
        source = Source(id="discovery-test", name="test", origin="user", recipe=run)
        try:
            result = await ADAPTERS[run.type].fetch(source, FetchState(), self.http)
        except Exception as e:  # any failure is the model's to fix, never the run's end
            # Fenced: adapter messages can quote what the server sent (a root element's name).
            return {"ok": False, "error": fence(_error(e))}
        if not result.items:
            return {"ok": False, "error": "the recipe ran but produced 0 items"}
        dates = [i.published_at for i in result.items if i.published_at]
        if dates and len(dates) == len(result.items) and self.now() - max(dates) > STALE_AFTER:
            return {"ok": False, "error": f"the newest item is from {max(dates):%Y-%m-%d}"}
        samples = [_sample(i) for i in result.items if _fits(i.url)][:SAMPLES]
        h = self.tested.add(parsed, samples)
        return {
            "ok": True,
            "recipe_hash": h,
            "items_found": len(result.items),
            "samples": fence([s.model_dump() for s in samples]),
        }
