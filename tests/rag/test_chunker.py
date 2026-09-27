from itertools import pairwise

from augury.rag.chunker import (
    CHARS_PER_TOKEN,
    MAX_TOKENS,
    archive_text,
    chunk_markdown,
    context_header,
)

SENTENCE = "Sparse attention keeps long contexts cheap to serve at scale. "  # 62 chars


def _chunks(md: str):
    return chunk_markdown(md, source_name="hf-blog", title="Long context")


def test_headings_start_sections_with_a_path_and_a_context_header():
    md = (
        "Opening words.\n\n# Method\n\nFirst part.\n\n## Training\n\nDetails here.\n\n"
        "# Results\n\nWins.\n"
    )
    chunks = _chunks(md)
    assert [c.section for c in chunks] == ["", "Method", "Method > Training", "Results"]
    assert chunks[2].context_header == "hf-blog · Long context · §Method > Training"
    assert chunks[0].context_header == "hf-blog · Long context"
    assert chunks[2].embed_text == "hf-blog · Long context · §Method > Training\n\nDetails here."


def test_offsets_point_back_into_the_markdown():
    md = "# A\n\nAlpha text.\n\n# B\n\nBeta text.\n"
    for c in _chunks(md):
        assert md[c.char_start : c.char_end] == c.text
    assert [c.text for c in _chunks(md)] == ["Alpha text.", "Beta text."]


def test_long_sections_become_400_to_600_token_passages_that_overlap():
    md = "# Method\n\n" + "\n\n".join(SENTENCE * 8 for _ in range(12))  # ~6k chars
    chunks = _chunks(md)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.text) <= MAX_TOKENS * CHARS_PER_TOKEN
    for c in chunks[:-1]:
        assert len(c.text) >= 400 * CHARS_PER_TOKEN * 0.75  # packed, not one paragraph each
    for before, after in pairwise(chunks):
        assert after.char_start < before.char_end  # a 60-token overlap
        assert before.char_end - after.char_start <= 60 * CHARS_PER_TOKEN


def test_code_blocks_and_tables_are_never_split():
    code = "```python\n" + "x = compute(1)\n" * 400 + "```"  # ~6k chars, past the limit
    table = "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 300
    md = f"# Code\n\nIntro.\n\n{code}\n\n# Table\n\n{table}\nAfter.\n"
    chunks = _chunks(md)
    assert sum(1 for c in chunks if code in c.text) == 1
    assert sum(1 for c in chunks if table.strip() in c.text) == 1
    assert not any(c.text.startswith("x = compute") for c in chunks)  # never mid-block


def test_a_heading_inside_a_code_block_is_code():
    md = "# Real\n\n```\n# not a heading\n```\n"
    chunks = _chunks(md)
    assert [c.section for c in chunks] == ["Real"] and "# not a heading" in chunks[0].text


def test_a_giant_paragraph_is_cut_at_sentence_ends():
    md = SENTENCE * 100  # one 6.2k-char paragraph
    chunks = _chunks(md)
    assert len(chunks) >= 3
    assert all(c.text.endswith(".") for c in chunks)
    assert all(len(c.text) <= MAX_TOKENS * CHARS_PER_TOKEN for c in chunks)


def test_empty_and_heading_only_documents_have_no_passages():
    assert _chunks("") == []
    assert _chunks("# Title only\n\n## And a subtitle\n") == []


def test_archive_text_and_header_collapse_whitespace():
    assert archive_text("A  title", " Summary. ", "HF Blog", ["rag", "agents"]) == (
        "A title\nSummary.\nSource: HF Blog\nTags: rag, agents"
    )
    assert archive_text("T", "", "S", []) == "T\nSource: S"
    assert context_header("HF\nBlog", "Two  words", "") == "HF Blog · Two words"
