from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SiteProfile:
    """Where the article lives on a known site, and which page chrome to cut out of it."""

    name: str
    hosts: frozenset[str]
    path_prefix: str
    container: str
    remove: tuple[str, ...]
    drop_block_after_title_containing: str | None = None

    def matches(self, url: str) -> bool:
        parts = urlsplit(url)
        host_ok = (parts.hostname or "").lower() in self.hosts
        return host_ok and parts.path.startswith(self.path_prefix)


# Verified against a live HF blog page on 2026-09-25: trafilatura kept 1 of 8 headings there.
HF_BLOG = SiteProfile(
    name="hf_blog",
    hosts=frozenset({"huggingface.co"}),
    path_prefix="/blog/",
    container="div.blog-content",
    remove=(
        ".not-prose",
        ":scope > [class*='SVELTE_HYDRATER']",
        ":scope > div.mb-4",
        "span.header-link",
        "script",
        "style",
        "svg",
        "button",
        "iframe",
    ),
    drop_block_after_title_containing="Published",
)

PROFILES: tuple[SiteProfile, ...] = (HF_BLOG,)


def profile_for(url: str) -> SiteProfile | None:
    return next((p for p in PROFILES if p.matches(url)), None)
