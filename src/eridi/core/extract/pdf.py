"""PDF text via PDFium (the engine in Chrome), through pypdfium2. It is many times faster
than pure-Python parsers on real-world PDFs, which dominate first-scan time.

Pages with almost no text suggest a scan that needs OCR. Very long PDFs are indexed up to
`MAX_PAGES`, so one huge file can't stall a scan.

Comments people add in Adobe Acrobat or other readers (sticky notes, text boxes, "insert
text" and strike-out edits) become extra lines such as `[Comment, page 3] Used 5% BSA`, so
adding or changing one shows up as a change. Plain highlights are left out on purpose: they
mark reading, not changes. A highlight with a typed comment keeps its comment.
"""

import ctypes
from typing import Any

from eridi.core.extract.base import ExtractResult, register

MIN_CHARS_PER_PAGE = 20
MAX_PAGES = 500

# PDF annotation subtypes (PDF 1.7 spec, table 169), as numbered by PDFium.
_TEXT, _FREETEXT, _HIGHLIGHT, _UNDERLINE, _SQUIGGLY, _STRIKEOUT, _CARET = 1, 3, 9, 10, 11, 12, 14
_COMMENT_KINDS = {_TEXT: "Comment", _FREETEXT: "Comment", _CARET: "Insert text"}
_MARKUP = {_HIGHLIGHT, _UNDERLINE, _SQUIGGLY}


def _annot_string(raw: Any, annot: Any, key: bytes) -> str:
    size = raw.FPDFAnnot_GetStringValue(annot, key, None, 0)
    if size <= 2:
        return ""
    buffer = ctypes.create_string_buffer(size)
    raw.FPDFAnnot_GetStringValue(
        annot, key, ctypes.cast(buffer, ctypes.POINTER(raw.FPDF_WCHAR)), size
    )
    return buffer.raw[:size].decode("utf-16-le", errors="replace").rstrip("\x00")


def _annotations(page: Any, textpage: Any, number: int) -> list[str]:
    """One line per comment-like annotation on a page."""
    import pypdfium2.raw as raw

    lines: list[str] = []
    for index in range(raw.FPDFPage_GetAnnotCount(page.raw)):
        annot = raw.FPDFPage_GetAnnot(page.raw, index)
        if not annot:
            continue
        try:
            subtype = raw.FPDFAnnot_GetSubtype(annot)
            contents = " ".join(_annot_string(raw, annot, b"Contents").split())
            author = " ".join(_annot_string(raw, annot, b"T").split())
            by = f" — {author}" if author else ""
            if subtype in _COMMENT_KINDS and contents:
                lines.append(f"[{_COMMENT_KINDS[subtype]}, page {number}] {contents}{by}")
            elif subtype == _STRIKEOUT:
                rect = raw.FS_RECTF()
                raw.FPDFAnnot_GetRect(annot, rect)
                struck = " ".join(
                    textpage.get_text_bounded(rect.left, rect.bottom, rect.right, rect.top).split()
                )
                detail = f"{struck}" + (f" — {contents}" if contents else "")
                if detail:
                    lines.append(f"[Strike-out, page {number}] {detail}{by}")
            elif subtype in _MARKUP and contents:
                lines.append(f"[Comment, page {number}] {contents}{by}")
        finally:
            raw.FPDFPage_CloseAnnot(annot)
    return lines


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
        comments: list[str] = []
        for index in range(min(count, MAX_PAGES)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_bounded().replace("\r\n", "\n").strip())
                comments += _annotations(page, textpage, index + 1)
            finally:
                textpage.close()
                page.close()
        title = (doc.get_metadata_dict().get("Title") or "").strip() or None
    finally:
        doc.close()

    text = "\n\n".join(p for p in pages if p)
    if title is None and text:
        title = text.splitlines()[0]
    if comments:
        text = (text + "\n\n" if text else "") + "\n".join(comments)
    reason = (
        f"Only the first {MAX_PAGES} of {count} pages are indexed." if count > MAX_PAGES else None
    )
    chars = sum(len(p) for p in pages)
    if not pages or chars / len(pages) < MIN_CHARS_PER_PAGE:
        return ExtractResult(text, title, "needs_ocr", "Little or no text layer; it may be a scan.")
    return ExtractResult(text, title, "ok", reason)
