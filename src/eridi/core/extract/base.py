"""Text extraction: the result type, and the registry of extractors by file extension.

Extractors never raise. Anything that goes wrong comes back as `status="error"` with a
reason, so one bad file never stops a scan. Reasons describe the failure, never the
file's contents.
"""

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

ExtractStatus = Literal[
    "ok", "needs_ocr", "cloud_document", "name_only", "too_large", "online_only", "error"
]
"""
- ok: text extracted
- needs_ocr: a PDF with little or no text layer (probably scanned)
- cloud_document: a Google Docs/Sheets/Slides shortcut; the content lives online (M12)
- name_only: a type Eridi can't read; only its name and path are indexed
- too_large: over the size limit; not read
- online_only: a sync client keeps it only in the cloud; not downloaded
- error: the file could not be read; `reason` says why
"""


@dataclass(frozen=True)
class ExtractResult:
    text: str
    title: str | None = None
    status: ExtractStatus = "ok"
    reason: str | None = None


Extractor = Callable[[bytes], ExtractResult]

READER_VERSION = 2
"""Bump whenever any extractor's output changes (2: Word tracked changes and comments, PDF
comments). Text read by an older version is never reused; files are read again instead."""

MAX_TEXT_CHARS = 2_000_000

_REGISTRY: dict[str, Extractor] = {}


def register(*extensions: str) -> Callable[[Extractor], Extractor]:
    """Register an extractor for extensions such as ".docx" (case-insensitive)."""

    def decorator(func: Extractor) -> Extractor:
        for ext in extensions:
            _REGISTRY[ext.lower()] = func
        return func

    return decorator


def extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def extractor_for(name: str) -> Extractor | None:
    return _REGISTRY.get(extension(name))


def registered_extensions() -> list[str]:
    return sorted(_REGISTRY)


def _describe(exc: BaseException) -> str:
    message = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    kind = type(exc).__name__
    return f"{kind}: {message}" if message else kind


def extract(name: str, data: bytes) -> ExtractResult:
    """Extract text from a file's bytes, choosing the extractor by the name's extension."""
    stem = PurePosixPath(name).stem
    func = extractor_for(name)
    if func is None:
        return ExtractResult("", stem, "name_only")
    try:
        with warnings.catch_warnings():
            # Parsers warn about features they skip (e.g. Excel data validation). That's
            # noise for users, and it scrambles the scan's progress line.
            warnings.simplefilter("ignore")
            result = func(data)
    except Exception as exc:
        return ExtractResult("", stem, "error", f"Could not read this file ({_describe(exc)}).")
    text = clean_text(result.text)
    reason = result.reason
    if len(text) > MAX_TEXT_CHARS:
        # Beyond this (a few books' worth), more text adds little to search but a lot of time.
        text = text[:MAX_TEXT_CHARS]
        reason = reason or f"Only the first {MAX_TEXT_CHARS:,} characters are indexed."
    title = (result.title or "").strip() or stem
    return ExtractResult(text, title[:300], result.status, reason)


def extract_file(
    name: str, size: int, read: Callable[[], bytes], size_limit: int | None
) -> ExtractResult:
    """Like `extract`, but checks the size limit before reading anything."""
    stem = PurePosixPath(name).stem
    if size_limit is not None and size > size_limit:
        mb = size_limit / (1024 * 1024)
        return ExtractResult("", stem, "too_large", f"Larger than the {mb:g} MB limit.")
    if extractor_for(name) is None:
        return ExtractResult("", stem, "name_only")
    try:
        data = read()
    except Exception as exc:
        return ExtractResult("", stem, "error", f"Could not open this file ({_describe(exc)}).")
    return extract(name, data)


def clean_text(text: str) -> str:
    """Normalize line endings, drop NUL characters and trailing spaces, trim blank edges."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def decode_text(data: bytes) -> str:
    """Decode bytes from a text file: UTF-8 (with or without BOM), UTF-16 with BOM, else cp1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")
