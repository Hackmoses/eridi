"""Google Drive shortcut files (.gdoc, .gsheet, .gslides).

On disk these are tiny JSON pointers; the document itself lives online. The Google Drive
connector (M12) reads the real content.
"""

from eridi.core.extract.base import ExtractResult, register


@register(".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform")
def extract_cloud_stub(data: bytes) -> ExtractResult:
    return ExtractResult(
        "",
        None,
        "cloud_document",
        "A Google Drive document. Connect Google Drive to search its text.",
    )
