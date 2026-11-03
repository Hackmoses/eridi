"""Snapshots: the extracted text of every file version Eridi has seen, so changes can be
diffed even for files whose storage keeps no history.

- Text lives at `<app-data>/snapshots/<first 2 hex>/<text_sha256>.txt.gz`, content-addressed,
  so identical text is stored once. Only extracted text is stored, never the original file.
- A size cap evicts the oldest snapshots first, but never one that is referenced (by a
  record entry's `change`, or as a file's current text).
- `versions.jsonl` is an append-only log of which file had which text when. It holds paths
  and hashes, never text. The index's `text_versions` and `file_moves` tables are rebuilt
  from it, which keeps the index disposable.
"""

import contextlib
import gzip
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from eridi.core.fsutil import atomic_write_bytes
from eridi.core.hashing import sha256_text
from eridi.core.paths import app_home

log = logging.getLogger(__name__)

LOG_NAME = "versions.jsonl"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VersionLine:
    """One line of the version log: a text version seen, or a file moved within a root."""

    kind: Literal["version", "move"]
    root_id: str
    path: str
    """For a move, the new path."""
    at: str
    text_sha256: str | None = None
    source: str = "scan"
    from_path: str | None = None

    def to_json(self) -> str:
        data: dict[str, Any] = {"kind": self.kind, "root_id": self.root_id, "path": self.path}
        data["at"] = self.at
        if self.kind == "version":
            data |= {"text_sha256": self.text_sha256, "source": self.source}
        else:
            data["from_path"] = self.from_path
        return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _parse_line(raw: str) -> VersionLine | None:
    try:
        data = json.loads(raw)
        kind = data["kind"]
        if kind == "version":
            if not _SHA_RE.match(data["text_sha256"]):
                return None
            return VersionLine(
                "version", data["root_id"], data["path"], data["at"],
                text_sha256=data["text_sha256"], source=data.get("source", "scan"),
            )  # fmt: skip
        if kind == "move":
            return VersionLine(
                "move", data["root_id"], data["path"], data["at"], from_path=data["from_path"]
            )
    except (ValueError, KeyError, TypeError):
        pass
    return None


class SnapshotStore:
    def __init__(self, folder: Path | None = None, cap_bytes: int = 500 * 1024 * 1024) -> None:
        self.folder = folder or app_home() / "snapshots"
        self.cap_bytes = cap_bytes
        self._logged: set[tuple[str, str, str]] | None = None
        self._pending: list[str] = []

    # --- text ---

    def path_for(self, sha: str) -> Path:
        if not _SHA_RE.match(sha):
            raise ValueError(f"{sha!r} is not a text hash.")
        return self.folder / sha[:2] / f"{sha}.txt.gz"

    def put(self, text: str) -> str:
        """Store text (once) and return its hash."""
        sha = sha256_text(text)
        target = self.path_for(sha)
        if not target.exists():
            data = gzip.compress(text.encode("utf-8"), mtime=0)
            with contextlib.suppress(FileExistsError):
                # Not fsynced: a snapshot lost in a power cut only means one diff is unavailable,
                # and skipping fsync keeps big first scans fast.
                atomic_write_bytes(target, data, overwrite=False, durable=False)
        return sha

    def get(self, sha: str) -> str | None:
        """The text for a hash, or None if it was never stored or has been evicted."""
        try:
            return gzip.decompress(self.path_for(sha).read_bytes()).decode("utf-8")
        except (FileNotFoundError, ValueError):
            return None
        except (OSError, EOFError, gzip.BadGzipFile):
            log.warning("A snapshot file is damaged and was ignored: %s", sha)
            return None

    def has(self, sha: str) -> bool:
        try:
            return self.path_for(sha).is_file()
        except ValueError:
            return False

    def _files(self) -> list[tuple[float, int, str, Path]]:
        out = []
        for path in self.folder.glob("??/*.txt.gz"):
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, path.name.removesuffix(".txt.gz"), path))
        return out

    def total_bytes(self) -> int:
        return sum(size for _, size, _, _ in self._files())

    def evict(self, referenced: Iterable[str]) -> list[str]:
        """Delete the oldest unreferenced snapshots until the store fits its cap."""
        keep = set(referenced)
        files = sorted(self._files())  # oldest first
        total = sum(size for _, size, _, _ in files)
        removed: list[str] = []
        for _, size, sha, path in files:
            if total <= self.cap_bytes:
                break
            if sha in keep:
                continue
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            total -= size
            removed.append(sha)
        return removed

    # --- version log ---

    @property
    def log_path(self) -> Path:
        return self.folder / LOG_NAME

    def read_log(self) -> list[VersionLine]:
        """Every valid line, in order. A torn or garbled line (e.g. after a crash) is skipped."""
        self.flush()
        try:
            raw = self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        lines = []
        for text in raw.splitlines():
            if text.strip():
                parsed = _parse_line(text)
                if parsed is None:
                    log.info("Skipped an unreadable line in the version log.")
                else:
                    lines.append(parsed)
        return lines

    def _append(self, line: VersionLine) -> None:
        self._pending.append(line.to_json() + "\n")

    def flush(self) -> None:
        """Write buffered log lines with a single fsync. Scans call this before committing."""
        if not self._pending:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        payload = "".join(self._pending)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            # A crash can leave a torn last line; start ours on a fresh line regardless.
            if handle.tell() > 0 and not self._ends_with_newline():
                payload = "\n" + payload
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._pending.clear()

    def _ends_with_newline(self) -> bool:
        with self.log_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"

    def log_version(
        self, root_id: str, path: str, text_sha256: str, at: str, source: str = "scan"
    ) -> bool:
        """Record that a file had this text. Returns False if that was already logged."""
        if self._logged is None:
            self._logged = {
                (ln.root_id, ln.path, ln.text_sha256 or "")
                for ln in self.read_log()
                if ln.kind == "version"
            }
        key = (root_id, path, text_sha256)
        if key in self._logged:
            return False
        self._append(VersionLine("version", root_id, path, at, text_sha256, source))
        self._logged.add(key)
        return True

    def log_move(self, root_id: str, from_path: str, to_path: str, at: str) -> None:
        self._append(VersionLine("move", root_id, to_path, at, from_path=from_path))
        self._logged = None  # moves re-key history; reload lazily


@dataclass(frozen=True)
class ReplayedVersion:
    text_sha256: str
    first_seen: str
    source: str


def replay(
    lines: Iterable[VersionLine],
) -> tuple[dict[tuple[str, str], dict[str, ReplayedVersion]], list[VersionLine]]:
    """Fold the log into per-file versions, following moves. Returns (versions, moves).

    When the same text is seen under two paths that later merge through a move, the earlier
    `first_seen` wins.
    """
    versions: dict[tuple[str, str], dict[str, ReplayedVersion]] = {}
    moves: list[VersionLine] = []
    for line in lines:
        key = (line.root_id, line.path)
        if line.kind == "version" and line.text_sha256 is not None:
            merge_version(versions.setdefault(key, {}), line.text_sha256, line.at, line.source)
        elif line.kind == "move" and line.from_path is not None:
            moves.append(line)
            moved = versions.pop((line.root_id, line.from_path), {})
            target = versions.setdefault(key, {})
            for v in moved.values():
                merge_version(target, v.text_sha256, v.first_seen, v.source)
    return versions, moves


def merge_version(into: dict[str, ReplayedVersion], sha: str, first_seen: str, source: str) -> None:
    old = into.get(sha)
    if old is None or first_seen < old.first_seen:
        into[sha] = ReplayedVersion(sha, first_seen, source)
