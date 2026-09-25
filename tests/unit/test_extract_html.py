import re
from pathlib import Path

import pytest

from augury.extract.base import ExtractionError
from augury.extract.html import extract_article_html

PAGES = Path(__file__).parents[1] / "fixtures" / "pages"
HF_URL = "https://huggingface.co/blog/someone/some-post"
BODY = " ".join(
    [
        "Speculative decoding lets a small draft model propose tokens that a larger"
        " model verifies in parallel, which cuts latency without changing outputs."
    ]
    * 8
)
GENERIC = f"""<html><head><title>Post</title></head><body>
<nav><a href="/">Home</a> <a href="/about">About us</a> Subscribe to our newsletter</nav>
<article><h1>Fast decoding</h1><p>{BODY}</p><h2>Results</h2><p>{BODY}</p></article>
<footer>Copyright Example Corp. All rights reserved.</footer></body></html>""".encode()


def test_hf_profile_keeps_headings_and_drops_chrome():
    result = extract_article_html((PAGES / "hf_blog_post.html").read_bytes(), HF_URL)
    assert result.extractor == "profile:hf_blog"
    assert result.body_md.startswith("# ")
    assert len(re.findall(r"^## ", result.body_md, re.M)) >= 2
    for chrome in ("Back to Articles", "Upvote", "Team Article"):
        assert chrome not in result.body_md
    assert result.word_count >= 200


def test_hf_profile_linked_images_are_not_corrupted():
    result = extract_article_html((PAGES / "hf_blog_post.html").read_bytes(), HF_URL)
    assert ")](" not in result.body_md
    placeholders = re.findall(r"\[image:[^\]]*\]\([^)]*\)", result.body_md)
    assert placeholders  # the fixture has images wrapped in "click to enlarge" links
    assert result.body_md.count("[image:") == len(placeholders)


def test_generic_page_uses_trafilatura_and_skips_boilerplate():
    result = extract_article_html(GENERIC, "https://example.com/post")
    assert result.extractor == "trafilatura"
    assert "draft model propose tokens" in result.body_md
    assert "Subscribe to our newsletter" not in result.body_md


def test_profile_falls_back_when_the_page_was_redesigned():
    result = extract_article_html(GENERIC, HF_URL)  # an HF URL without the expected container
    assert result.extractor == "trafilatura"


def test_too_little_text_is_an_error():
    with pytest.raises(ExtractionError, match="too little text"):
        extract_article_html(b"<html><body><p>Hi.</p></body></html>", "https://example.com/x")
