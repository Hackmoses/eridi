"""PDF text via PDFium (the engine in Chrome), through pypdfium2. It is many times faster
than pure-Python parsers on real-world PDFs, which dominate first-scan time.

Pages with almost no text suggest a scan that needs OCR. Very long PDFs are indexed up to
`MAX_PAGES`, so one huge file can't stall a scan.
"""

from eridi.core.extract.base import ExtractResult, register

MIN_CHARS_PER_PAGE = 20
MAX_PAGES = 500


@register(".pdf")
def extract_pdf(data: bytes) -> ExtractResult:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            return ExtractResult("", None, "error", "The PDF is password-protected.")
        raise
    try:
        count = len(doc)
        pages: list[str] = []
        for index in range(min(count, MAX_PAGES)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_bounded().replace("\r\n", "\n").strip())
            finally:
                textpage.close()
                page.close()
        title = (doc.get_metadata_dict().get("Title") or "").strip() or None
    finally:
        doc.close()

    text = "\n\n".join(p for p in pages if p)
    if title is None and text:
        title = text.splitlines()[0]
    reason = (
        f"Only the first {MAX_PAGES} of {count} pages are indexed." if count > MAX_PAGES else None
    )
    chars = sum(len(p) for p in pages)
    if not pages or chars / len(pages) < MIN_CHARS_PER_PAGE:
        return ExtractResult(text, title, "needs_ocr", "Little or no text layer; it may be a scan.")
    return ExtractResult(text, title, "ok", reason)
