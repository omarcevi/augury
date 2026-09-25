import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n; drops \r
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid"}
_ARXIV = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/|\barxiv:\s*)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)


def strip_control_chars(value: str) -> str:
    # CRLF becomes LF first so line breaks survive; a lone \r (line overwrite) is dropped.
    # ANSI sequences go before other controls, otherwise removing a bare ESC leaves "[31m".
    return _CONTROL.sub("", _ANSI.sub("", value.replace("\r\n", "\n")))


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip()
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    default_port = (scheme, port) in {("http", 80), ("https", 443)}
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_arxiv_id(*texts: str | None) -> str | None:
    for text in texts:
        if text and (match := _ARXIV.search(text)):
            return match.group(1)
    return None


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()


def item_id_for(kind: str, url: str, arxiv_id: str | None) -> str:
    if kind == "paper" and arxiv_id:
        return f"arxiv:{arxiv_id}"
    return "web:" + _sha1(canonical_url(url))[:16]


def content_hash(title: str, summary: str) -> str:
    return _sha1(f"{title}\n{summary}")


def slugify(name: str, max_len: int = 63) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:max_len].strip("-")
    return slug or "source"
