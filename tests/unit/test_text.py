from augury.core.text import (
    canonical_url,
    content_hash,
    extract_arxiv_id,
    item_id_for,
    slugify,
    strip_control_chars,
    with_default_scheme,
)


def test_strip_control_chars_removes_ansi_and_c0_but_keeps_text():
    raw = "Red \x1b[31mTitle\x1b[0m\x00 with\ttab\n中文 🚀 \x1b]8;;http://x\x07link"
    assert strip_control_chars(raw) == "Red Title with\ttab\n中文 🚀 link"


def test_carriage_returns_are_removed_but_crlf_becomes_a_newline():
    assert strip_control_chars("Legit Title\rOVERWRITTEN") == "Legit TitleOVERWRITTEN"
    assert strip_control_chars("line one\r\nline two") == "line one\nline two"


def test_canonical_url_drops_tracking_fragment_and_trailing_slash():
    a = canonical_url("HTTPS://Example.com:443/Blog/Post/?utm_source=x&b=2&a=1&fbclid=9#top")
    assert a == "https://example.com/Blog/Post?a=1&b=2"
    assert canonical_url("https://example.com") == "https://example.com/"


def test_canonical_url_keeps_non_default_port():
    assert canonical_url("http://example.com:8080/x") == "http://example.com:8080/x"


def test_canonical_url_of_a_bare_domain_assumes_https():
    assert canonical_url("Example.COM/Path/") == "https://example.com/Path"
    assert canonical_url("example.com") == "https://example.com/"


def test_extract_arxiv_id_from_urls_and_text():
    assert extract_arxiv_id("https://arxiv.org/abs/2609.24984v2") == "2609.24984"
    assert extract_arxiv_id(None, "see https://huggingface.co/papers/2412.20138") == "2412.20138"
    assert extract_arxiv_id("arXiv: 2501.00001") == "2501.00001"
    assert extract_arxiv_id("no id here") is None


def test_item_ids():
    assert item_id_for("paper", "https://huggingface.co/papers/2609.24984", "2609.24984") == (
        "arxiv:2609.24984"
    )
    a = item_id_for("article", "https://x.com/post?utm_source=feed", None)
    b = item_id_for("article", "https://x.com/post/", None)
    assert a == b and a.startswith("web:") and len(a) == len("web:") + 16


def test_with_default_scheme_looks_for_an_existing_scheme_only_before_the_path():
    # P5.4: a bare "//" anywhere in the URL (e.g. inside a query value) used to be read as
    # "this already has a scheme", so no https:// was added.
    assert with_default_scheme("jvns.ca/?next=https://x") == "https://jvns.ca/?next=https://x"
    assert with_default_scheme("example.com/a?b=http://c") == "https://example.com/a?b=http://c"


def test_with_default_scheme_leaves_real_schemes_and_protocol_relative_urls_alone():
    assert with_default_scheme("http://example.com/path") == "http://example.com/path"
    assert with_default_scheme("//cdn.example.com/x") == "//cdn.example.com/x"
    assert with_default_scheme("mailto:a@b.com") == "mailto:a@b.com"


def test_with_default_scheme_still_adds_https_for_a_bare_domain_or_host_port():
    assert with_default_scheme("example.com/path") == "https://example.com/path"
    assert with_default_scheme("host:8080/path") == "https://host:8080/path"


def test_content_hash_and_slugify():
    assert content_hash("t", "s") == content_hash("t", "s") != content_hash("t", "S")
    assert slugify("Google Research Blog!") == "google-research-blog"
    assert slugify("!!!") == "source"
