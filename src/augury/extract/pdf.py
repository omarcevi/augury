import io
import re

from pdfminer.high_level import extract_text

from augury.extract.base import MIN_WORDS, ExtractionError

NOTICE = "> Extracted from the PDF; layout, math and tables may be imperfect."


def pdf_to_markdown(data: bytes) -> str:
    try:
        text = extract_text(io.BytesIO(data))
    except Exception as e:  # pdfminer raises many parser-specific exceptions
        raise ExtractionError(f"could not read the PDF: {type(e).__name__}") from e
    text = re.sub(r"-\n(?=[a-z])", "", text)  # re-join words hyphenated across lines
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if p]
    if sum(len(p.split()) for p in paragraphs) < MIN_WORDS:
        raise ExtractionError("the PDF has no extractable text (is it scanned images?)")
    return NOTICE + "\n\n" + "\n\n".join(paragraphs)
