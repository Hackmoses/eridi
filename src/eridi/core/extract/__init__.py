"""Text extraction from the files labs keep. Importing this package registers every extractor."""

from eridi.core.extract import office, pdf, plain, stubs
from eridi.core.extract.base import (
    ExtractResult,
    ExtractStatus,
    extension,
    extract,
    extract_file,
    extractor_for,
    registered_extensions,
)

__all__ = [
    "ExtractResult",
    "ExtractStatus",
    "extension",
    "extract",
    "extract_file",
    "extractor_for",
    "office",
    "pdf",
    "plain",
    "registered_extensions",
    "stubs",
]
