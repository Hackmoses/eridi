"""Index one storage root: list its files, read and extract each, store them in the index.

This is the simple, always-complete path used by rebuilds. The scanner (M4) adds change
detection and snapshots on top of the same steps.
"""

import logging
import sqlite3
from dataclasses import dataclass, field

from eridi.connectors.base import Connector, ConnectorError, RemoteFile
from eridi.core.extract import ExtractResult, extension, extract
from eridi.core.hashing import sha256_bytes, sha256_text
from eridi.core.index.files import list_files, set_status, upsert_file, upsert_root
from eridi.core.models import FileInfo, Root, utc_now
from eridi.core.snapshots import SnapshotStore

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    indexed: int = 0
    missing: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason) for files that could not be read or extracted."""


def read_and_extract(
    connector: Connector, remote: RemoteFile, size_limit: int | None, *, read_placeholders: bool
) -> tuple[str | None, ExtractResult]:
    """Return (content_sha256, extraction) for one file, without ever raising."""
    name = remote.path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if remote.is_placeholder and not read_placeholders:
        return remote.content_sha256, ExtractResult(
            "", stem, "name_only", "Online-only; not downloaded."
        )
    if size_limit is not None and remote.size > size_limit:
        mb = size_limit / (1024 * 1024)
        return remote.content_sha256, ExtractResult(
            "", stem, "too_large", f"Larger than the {mb:g} MB limit."
        )
    try:
        data = connector.open_bytes(remote.path)
    except (ConnectorError, OSError, ValueError) as exc:
        return remote.content_sha256, ExtractResult("", stem, "error", str(exc))
    return sha256_bytes(data), extract(name, data)


def index_root(
    conn: sqlite3.Connection,
    root: Root,
    connector: Connector,
    *,
    size_limit: int | None,
    read_placeholders: bool = False,
    snapshots: SnapshotStore | None = None,
) -> IngestReport:
    """Index every file in a root, and mark files that are no longer there as missing."""
    report = IngestReport()
    seen: set[str] = set()
    now = utc_now()
    with conn:
        upsert_root(conn, root)
    for remote in connector.list_files():
        seen.add(remote.path)
        content_sha, result = read_and_extract(
            connector, remote, size_limit, read_placeholders=read_placeholders
        )
        if result.status == "error":
            report.errors.append((remote.path, result.reason or "unknown error"))
            log.info("Could not extract %s: %s", remote.path, result.reason)
        info = FileInfo(
            root_id=root.id,
            path=remote.path,
            size=remote.size,
            modified=remote.modified,
            ext=extension(remote.path),
            content_sha256=content_sha,
            text_sha256=sha256_text(result.text) if result.text else None,
            placeholder=remote.is_placeholder,
            title=result.title,
            extract_status=result.status,
            last_seen=now,
        )
        if snapshots is not None and result.text:
            snapshots.put(result.text)
            snapshots.log_version(root.id, remote.path, sha256_text(result.text), now)
        with conn:
            upsert_file(conn, info, result.text)
        report.indexed += 1
    with conn:
        for known in list_files(conn, root.id):
            if known.path not in seen and known.status != "missing":
                set_status(conn, root.id, known.path, "missing")
                report.missing += 1
    return report
