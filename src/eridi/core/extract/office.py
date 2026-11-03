"""Microsoft Office files: .docx, .xlsx, .pptx.

One line per paragraph, and table rows as `cell | cell`, so line diffs (M4) read naturally.
"""

import io
from collections.abc import Iterable
from typing import Any

from eridi.core.extract.base import ExtractResult, register

MAX_XLSX_CELLS = 20_000


def _row(cells: Iterable[str]) -> str:
    values: list[str] = []
    for cell in cells:
        value = " ".join(cell.split())
        # Merged cells repeat their text; keep it once.
        if values and value == values[-1]:
            continue
        values.append(value)
    while values and not values[-1]:
        values.pop()
    return " | ".join(values)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w_text(element: Any, tag: str = "t") -> str:
    """The text of a Word XML element, leaving out text deleted with Track Changes."""
    parts = []
    for node in element.iter(_W + tag, _W + "tab", _W + "br"):
        if tag == "t" and any(a.tag == _W + "del" for a in node.iterancestors()):
            continue
        if node.tag == _W + tag:
            parts.append(node.text or "")
        else:
            parts.append(" ")
    return " ".join("".join(parts).split())


def _docx_markup(doc: Any) -> list[str]:
    """Tracked changes and comments, one line each, so edits to them show as changes.

    Paragraph text already reads like the document with insertions accepted; these lines
    say what was tracked, and what each comment says."""
    lines: list[str] = []
    body = doc.element.body
    for change in body.iter(_W + "ins", _W + "del"):
        if change.tag == _W + "ins":
            text = _w_text(change)
            label = "Tracked insertion"
        else:
            text = _w_text(change, "delText")
            label = "Tracked deletion"
        if text:
            author = change.get(_W + "author")
            lines.append(f"[{label}] {text}" + (f" — {author}" if author else ""))
    try:
        comments = list(doc.comments)
    except Exception:  # documents without a comments part, or older python-docx
        comments = []
    for comment in comments:
        text = " ".join(comment.text.split())
        if text:
            author = comment.author
            lines.append(f"[Comment] {text}" + (f" — {author}" if author else ""))
    return lines


@register(".docx", ".docm")
def extract_docx(data: bytes) -> ExtractResult:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    first_heading: str | None = None
    first_line: str | None = None

    def add_table(table: Table) -> None:
        for row in table.rows:
            line = _row(" ".join(_w_text(p) for p in cell._tc.iter(_W + "p")) for cell in row.cells)
            if line:
                lines.append(line)

    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            # Read the XML ourselves: python-docx's paragraph.text drops text typed with
            # Track Changes on.
            text = _w_text(block._p)
            if not text:
                continue
            lines.append(text)
            first_line = first_line or text
            # The raw style ID ("Heading1", "Title") is cheap; `block.style` searches every
            # style in the document for each paragraph and dominated scan time.
            style_id = block._p.style or "" if first_heading is None else ""
            if first_heading is None and style_id.startswith(("Heading", "Title")):
                first_heading = text
        else:
            add_table(block)
    lines += _docx_markup(doc)
    title = doc.core_properties.title or first_heading or first_line
    return ExtractResult("\n".join(lines), title)


@register(".xlsx", ".xlsm")
def extract_xlsx(data: bytes) -> ExtractResult:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        lines: list[str] = []
        cells = 0
        truncated = False
        for sheet in workbook.worksheets:
            if truncated:
                break
            lines.append(sheet.title)
            for row in sheet.iter_rows(values_only=True):
                values = ["" if v is None else str(v) for v in row]
                filled = sum(1 for v in values if v)
                if cells + filled > MAX_XLSX_CELLS:
                    truncated = True
                    break
                cells += filled
                line = _row(values)
                if line:
                    lines.append(line)
        title = workbook.properties.title if workbook.properties else None
    finally:
        workbook.close()
    reason = f"Only the first {MAX_XLSX_CELLS:,} cells are indexed." if truncated else None
    return ExtractResult("\n".join(lines), title, "ok", reason)


@register(".pptx", ".pptm")
def extract_pptx(data: bytes) -> ExtractResult:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    lines: list[str] = []
    title: str | None = None
    for slide in presentation.slides:
        title_shape = slide.shapes.title
        if title is None and title_shape is not None and title_shape.has_text_frame:
            title = title_shape.text_frame.text.strip() or None
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if text:
                        lines.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    line = _row(cell.text for cell in row.cells)
                    if line:
                        lines.append(line)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(notes)
    core_title = presentation.core_properties.title
    return ExtractResult("\n".join(lines), core_title or title)
