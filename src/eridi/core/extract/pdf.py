"""PDF text via pypdf. Pages with almost no text suggest a scan that needs OCR."""

import io
import logging

from eridi.core.extract.base import ExtractResult, register

MIN_CHARS_PER_PAGE = 20


@register(".pdf")
def extract_pdf(data: bytes) -> ExtractResult:
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # its warnings are noise for users
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        return ExtractResult("", None, "error", "The PDF is password-protected.")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    title = None
    meta = reader.metadata
    if meta is not None and meta.title:
        title = str(meta.title)
    elif text:
        title = text.splitlines()[0]
    chars = sum(len(p) for p in pages)
    if not pages or chars / len(pages) < MIN_CHARS_PER_PAGE:
        return ExtractResult(text, title, "needs_ocr", "Little or no text layer; it may be a scan.")
    return ExtractResult(text, title)
