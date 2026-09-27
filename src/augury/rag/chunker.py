"""Chunking (spec §6.2). Full text is split on markdown headings, then packed into passages of
400-600 tokens (estimated as chars / 4) with a 60-token overlap between neighbors in the same
section. Code blocks and tables are never split, even past the size limit. Every passage keeps
its offsets into the markdown and a context header, "{source} · {title} · §{section path}",
that is embedded and indexed with it. The archive tier is one passage per item."""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

CHUNKER_VERSION = 1  # bump when chunking changes, so content chunks are rebuilt
CHARS_PER_TOKEN = 4
TARGET_TOKENS = 500
MAX_TOKENS = 600
OVERLAP_TOKENS = 60
SECTION_SEP = " > "

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

BlockKind = Literal["prose", "code", "table"]


@dataclass(frozen=True)
class Chunk:
    section: str  # "Method > Training"; "" before the first heading
    text: str  # the passage itself: markdown[char_start:char_end]
    char_start: int
    char_end: int
    context_header: str

    @property
    def embed_text(self) -> str:
        """What is embedded: the header first, so a passage keeps its title and section."""
        return f"{self.context_header}\n\n{self.text}"


@dataclass(frozen=True)
class _Block:
    kind: BlockKind
    start: int
    end: int


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def context_header(source_name: str, title: str, section: str = "") -> str:
    parts = [" ".join(source_name.split()), " ".join(title.split())]
    if section:
        parts.append(f"§{section}")
    return " · ".join(p for p in parts if p)


def archive_text(title: str, summary: str, source_name: str, tags: Sequence[str]) -> str:
    """The archive tier's one passage (spec §6.1): title, summary, source name and tags."""
    lines = [" ".join(title.split()), summary.strip(), f"Source: {source_name}"]
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    return "\n".join(line for line in lines if line)


def _lines(md: str) -> Iterator[tuple[int, str]]:
    offset = 0
    for line in md.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def _sections(md: str) -> Iterator[tuple[str, list[_Block]]]:
    """(section path, its blocks) in document order; heading lines belong to no block."""
    stack: list[tuple[int, str]] = []
    blocks: list[_Block] = []
    lines = list(_lines(md))
    i = 0
    while i < len(lines):
        start, line = lines[i]
        stripped = line.strip()
        if fence := _FENCE.match(line):
            marker = fence.group(1)
            j = i + 1
            while j < len(lines) and not lines[j][1].strip().startswith(marker[0] * len(marker)):
                j += 1
            end_line = min(j, len(lines) - 1)
            blocks.append(_Block("code", start, lines[end_line][0] + len(lines[end_line][1])))
            i = end_line + 1
            continue
        if heading := _HEADING.match(stripped):
            yield SECTION_SEP.join(title for _, title in stack), blocks
            blocks = []
            level, title = len(heading.group(1)), heading.group(2).strip()
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
            i += 1
            continue
        if not stripped:
            i += 1
            continue
        kind: BlockKind = "table" if stripped.startswith("|") else "prose"
        j = i + 1
        while j < len(lines):
            nxt = lines[j][1].strip()
            if not nxt or _HEADING.match(nxt) or _FENCE.match(lines[j][1]):
                break
            if (kind == "table") != nxt.startswith("|"):
                break
            j += 1
        blocks.append(_Block(kind, start, lines[j - 1][0] + len(lines[j - 1][1])))
        i = j
    yield SECTION_SEP.join(title for _, title in stack), blocks


def _split_prose(md: str, block: _Block, limit: int) -> list[_Block]:
    """A paragraph longer than `limit` chars, cut at sentence ends (else at spaces)."""
    pieces: list[_Block] = []
    start = block.start
    text = md[block.start : block.end]
    cuts = [block.start + m.end() for m in _SENTENCE_END.finditer(text)] + [block.end]
    last = block.start
    for cut in cuts:
        if cut - start > limit and last > start:
            pieces.append(_Block("prose", start, last))
            start = last
        while cut - start > limit:  # one sentence longer than the limit: cut at a space
            space = md.rfind(" ", start, start + limit)
            stop = space if space > start else start + limit
            pieces.append(_Block("prose", start, stop))
            start = stop
        last = cut
    if start < block.end:
        pieces.append(_Block("prose", start, block.end))
    return pieces


def _overlap_after(md: str, block: _Block, chars: int) -> int | None:
    """Where the next passage starts when `block` ended the last one: its last `chars`, from a
    word boundary. None after a code block or a table, which are never cut."""
    if block.kind != "prose":
        return None
    if block.end - block.start <= chars:
        return block.start
    space = md.find(" ", block.end - chars, block.end)
    return space + 1 if space != -1 else block.end - chars


def chunk_markdown(
    md: str,
    *,
    source_name: str,
    title: str,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    target, limit = target_tokens * CHARS_PER_TOKEN, max_tokens * CHARS_PER_TOKEN
    overlap = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []

    def emit(section: str, begin: int, end: int) -> None:
        raw = md[begin:end]
        start, stop = begin + len(raw) - len(raw.lstrip()), end - len(raw) + len(raw.rstrip())
        if stop > start:
            header = context_header(source_name, title, section)
            chunks.append(Chunk(section, md[start:stop], start, stop, header))

    for section, blocks in _sections(md):
        begin: int | None = None  # where the passage being built starts (maybe an overlap)
        last: _Block | None = None  # its last block
        for original in blocks:
            too_long = original.kind == "prose" and original.end - original.start > limit
            for block in _split_prose(md, original, target) if too_long else [original]:
                if last is not None and begin is not None and block.end - begin > limit:
                    emit(section, begin, last.end)
                    begin, last = _overlap_after(md, last, overlap), None
                if begin is None:
                    begin = block.start
                last = block
                if block.end - begin >= target:
                    emit(section, begin, block.end)
                    begin, last = _overlap_after(md, block, overlap), None
        if last is not None and begin is not None:
            emit(section, begin, last.end)
    return chunks
