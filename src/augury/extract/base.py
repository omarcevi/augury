from dataclasses import dataclass

MIN_WORDS = 50


class ExtractionError(Exception):
    """The page was fetched, but no readable article came out of it."""


class UnsupportedItem(Exception):
    pass


@dataclass(frozen=True)
class Extracted:
    body_md: str
    extractor: str
    word_count: int
