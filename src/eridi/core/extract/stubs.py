"""Google documents (.gdoc, .gsheet, .gslides, .gdraw, .gform).

In a synced folder these are tiny JSON shortcuts; the document itself lives online. Read
through the Google Drive connector (M12), Docs, Sheets and Slides arrive as exported text
(Sheets as CSV) under the same names, and are read as text.
"""

import json

from eridi.core.extract.base import ExtractResult, decode_text, register


def _is_shortcut(data: bytes) -> bool:
    try:
        stub = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(stub, dict) and any(k in stub for k in ("doc_id", "resource_id", "url"))


@register(".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform")
def extract_google_document(data: bytes) -> ExtractResult:
    if not data.strip() or _is_shortcut(data):
        return ExtractResult(
            "",
            None,
            "cloud_document",
            "A Google Drive document. Connect Google Drive to search its text.",
        )
    return ExtractResult(decode_text(data))
