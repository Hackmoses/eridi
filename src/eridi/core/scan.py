"""Scan storage roots: find new, changed, moved and removed files, extract their text and
snapshot every new text version.

Cheap by design: a file is re-read only when its size or modified time changed, and its
text is extracted only when its bytes changed. Removed files are marked missing, never
deleted, so their history stays. A file that disappears while another file with the same
bytes appears is a move, and its history follows it.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from eridi.connectors.base import Connector, ConnectorError, RemoteFile
from eridi.core.config import Settings
from eridi.core.extract import ExtractResult, extension, extract, extractor_for
from eridi.core.hashing import sha256_bytes, sha256_text
from eridi.core.index.files import list_files, move_file, set_status, upsert_file, upsert_root
from eridi.core.models import FileInfo, Root, utc_now
from eridi.core.snapshots import SnapshotStore

log = logging.getLogger(__name__)

COMMIT_EVERY = 50

Progress = Callable[[int, str], None]
"""Called before each file with (files done so far, the file's path)."""


@dataclass
class ScanReport:
    root_id: str
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    """Files whose text changed."""
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason): too large, online-only."""
    errors: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason): files that could not be read or extracted."""
    extracted: int = 0
    unavailable: str | None = None
    """Why the whole root could not be scanned (e.g. a drive that isn't connected)."""


def _stem(path: str) -> str:
    return PurePosixPath(path).stem


def read_bytes(
    connector: Connector, remote: RemoteFile, size_limit: int | None, *, read_placeholders: bool
) -> tuple[bytes | None, ExtractResult | None]:
    """The file's bytes, or (None, a result saying why it wasn't read). Never raises."""
    stem = _stem(remote.path)
    if remote.is_placeholder and not read_placeholders:
        return None, ExtractResult("", stem, "online_only", "Online-only; not downloaded.")
    if size_limit is not None and remote.size > size_limit:
        mb = size_limit / (1024 * 1024)
        return None, ExtractResult("", stem, "too_large", f"Larger than the {mb:g} MB limit.")
    try:
        return connector.open_bytes(remote.path), None
    except (ConnectorError, OSError, ValueError) as exc:
        return None, ExtractResult("", stem, "error", str(exc))


def read_and_extract(
    connector: Connector, remote: RemoteFile, size_limit: int | None, *, read_placeholders: bool
) -> tuple[str | None, ExtractResult]:
    """(content_sha256, extraction) for one file. Never raises."""
    data, skipped = read_bytes(connector, remote, size_limit, read_placeholders=read_placeholders)
    if data is None:
        assert skipped is not None  # noqa: S101 - read_bytes returns one or the other
        return remote.content_sha256, skipped
    return sha256_bytes(data), extract(remote.path.rsplit("/", 1)[-1], data)


def scan_root(
    conn: sqlite3.Connection,
    root: Root,
    connector: Connector,
    snapshots: SnapshotStore,
    settings: Settings | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    settings = settings or Settings()
    report = ScanReport(root.id)
    now = utc_now()
    with conn:
        upsert_root(conn, root)
    existing = {f.path: f for f in list_files(conn, root.id)}
    seen: set[str] = set()
    new_files: list[FileInfo] = []
    pending = 0
    try:
        for done, remote in enumerate(connector.list_files()):
            if progress is not None:
                progress(done, remote.path)
            seen.add(remote.path)
            stored = _scan_file(conn, root, connector, remote, existing.get(remote.path), now,
                                snapshots, settings, report)  # fmt: skip
            if stored is not None:
                new_files.append(stored)
            pending += 1
            if pending >= COMMIT_EVERY:
                conn.commit()
                pending = 0
    except ConnectorError as exc:
        conn.commit()
        report.unavailable = str(exc)
        return report  # don't mark anything missing when the root itself is unreachable

    gone = {p: f for p, f in existing.items() if p not in seen and f.status == "present"}
    by_content: dict[str, FileInfo] = {}
    for f in gone.values():
        if f.content_sha256:
            by_content.setdefault(f.content_sha256, f)
    for new in new_files:
        old = by_content.pop(new.content_sha256 or "", None)
        if old is None or old.path not in gone:
            continue
        del gone[old.path]
        move_file(conn, old, new, now)
        snapshots.log_move(root.id, old.path, new.path, now)
        report.new.remove(new.path)
        report.moved.append((old.path, new.path))
    for path in sorted(gone):
        set_status(conn, root.id, path, "missing")
        report.missing.append(path)
    conn.commit()
    return report


def _scan_file(
    conn: sqlite3.Connection,
    root: Root,
    connector: Connector,
    remote: RemoteFile,
    old: FileInfo | None,
    now: str,
    snapshots: SnapshotStore,
    settings: Settings,
    report: ScanReport,
) -> FileInfo | None:
    """Scan one file. Returns the stored row if the file is new (a possible move target)."""
    present = old is not None and old.status == "present"
    if (
        old is not None
        and present
        and old.size == remote.size
        and old.modified == remote.modified
        and old.placeholder == remote.is_placeholder
    ):
        report.unchanged += 1
        return None

    if extractor_for(remote.path) is None:
        # Nothing to search in it (images, videos, archives…): index its name without
        # reading it, so scanning a folder full of media stays fast.
        data: bytes | None = None
        skipped: ExtractResult | None = ExtractResult("", _stem(remote.path), "name_only")
    else:
        data, skipped = read_bytes(
            connector, remote, settings.size_limit_bytes,
            read_placeholders=settings.read_online_only_files,
        )  # fmt: skip
    content_sha = sha256_bytes(data) if data is not None else remote.content_sha256
    base = FileInfo(
        root_id=root.id,
        path=remote.path,
        size=remote.size,
        modified=remote.modified,
        ext=extension(remote.path),
        content_sha256=content_sha,
        placeholder=remote.is_placeholder,
        last_seen=now,
    )

    if (
        old is not None
        and present
        and content_sha is not None
        and content_sha == old.content_sha256
    ):
        # Touched or re-saved with identical bytes: update the metadata, keep the text.
        upsert_file(conn, _keep_text(base, old), None)
        report.unchanged += 1
        return None

    if skipped is not None:
        result = skipped
        if result.status == "error":
            report.errors.append((remote.path, result.reason or "unknown error"))
            if old is not None and present:
                return None  # keep what we had; a read error is not a change
        elif result.status != "name_only":
            report.skipped.append((remote.path, result.reason or result.status))
    else:
        assert data is not None  # noqa: S101
        result = extract(remote.path.rsplit("/", 1)[-1], data)
        report.extracted += 1
        if result.status == "error":
            report.errors.append((remote.path, result.reason or "unknown error"))
            log.info("Could not extract %s: %s", remote.path, result.reason)

    text_sha = sha256_text(result.text) if result.text else None
    info = replace(base, text_sha256=text_sha, title=result.title, extract_status=result.status)
    old_text = old.text_sha256 if old is not None else None
    if result.text and text_sha != old_text:
        snapshots.put(result.text)
        snapshots.log_version(root.id, remote.path, text_sha or "", now)
    stored = upsert_file(conn, info, result.text)

    if old is None:
        report.new.append(remote.path)
        return stored
    if not present:
        report.new.append(remote.path)  # it came back
    elif text_sha != old_text:
        report.changed.append(remote.path)
    else:
        report.unchanged += 1
    return None


def _keep_text(base: FileInfo, old: FileInfo) -> FileInfo:
    return replace(
        base, text_sha256=old.text_sha256, title=old.title, extract_status=old.extract_status
    )


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Root],
    connectors: dict[str, Connector],
    snapshots: SnapshotStore,
    settings: Settings | None = None,
    progress: Progress | None = None,
) -> list[ScanReport]:
    """Scan every root with search on."""
    return [
        scan_root(conn, root, connectors[root.id], snapshots, settings, progress)
        for root in roots
        if root.search
    ]
