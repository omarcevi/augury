from augury.extract.markdown_utils import images_to_placeholders, lead_paragraph, tidy, word_count


def test_images_become_openable_placeholders():
    md = 'Intro ![Fig. 2 — architecture](https://x/f2.png "title") and ![](https://x/a.png)'
    assert images_to_placeholders(md) == (
        "Intro [image: Fig. 2 — architecture](https://x/f2.png) and [image: figure](https://x/a.png)"
    )


def test_linked_image_collapses_to_one_placeholder():
    # markdownify emits [![alt](src)](href) for a "click to enlarge" image link; CommonMark
    # doesn't allow a link inside a link, so keeping both corrupts the text.
    assert images_to_placeholders("[![a](i.png)](big.png)") == "[image: a](i.png)"
    assert images_to_placeholders("![a](i.png)") == "[image: a](i.png)"  # bare image: unchanged


def test_bracketed_alt_text_from_latexml_is_unwrapped():
    # LaTeXML's default alt text for an uncaptioned figure is literally "[Uncaptioned image]".
    assert images_to_placeholders("![[Uncaptioned image]](u.png)") == (
        "[image: Uncaptioned image](u.png)"
    )
    assert images_to_placeholders("[![[x]](i.png)](big.png)") == "[image: x](i.png)"


def test_tidy_collapses_blank_runs_and_trailing_space():
    assert tidy("a  \n\n\n\nb\n") == "a\n\nb"


def test_word_count_ignores_link_targets():
    assert word_count("Read [the paper](https://arxiv.org/abs/1234.5678) now") == 4


LONG = "Speculative decoding lets a small draft model propose tokens for a larger model to verify."


def test_lead_paragraph_skips_headings_images_and_short_lines():
    md = f"# Title\n\nShort line.\n\n[image: fig](https://x/f.png)\n\n{LONG} See [paper](https://a/b)."
    assert lead_paragraph(md) == f"{LONG} See paper."


def test_lead_paragraph_unescapes_markdown_and_truncates():
    md = " ".join([LONG.replace("draft", r"draft\_model")] * 3)  # one ~290-char paragraph
    summary = lead_paragraph(md, max_chars=120)
    assert "draft_model" in summary and len(summary) <= 120 and summary.endswith("…")


def test_lead_paragraph_can_be_empty():
    assert lead_paragraph("# Only a heading") == ""
