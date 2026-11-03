"""Local and synced folders (including OneDrive, Google Drive and Dropbox sync folders).

Read-only: files are only ever opened with mode "rb". Online-only placeholders that a sync
client keeps in the cloud are listed but not read, unless the user turns on
`read_online_only_files`, so a scan never downloads someone's whole cloud drive.
"""

import logging
import os
import stat as stat_module
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import pathspec

from eridi.connectors.base import (
    ConnectorError,
    PlaceholderNotReadError,
    RemoteChange,
    RemoteFile,
    RemoteVersion,
)
from eridi.core.paths import normalize_relative

log = logging.getLogger(__name__)

IGNORE_FILE = ".eridiignore"
ROOT_MARKER = ".eridi-root.json"
DEFAULT_IGNORES: tuple[str, ...] = (
    # version control, environments and caches
    ".git/",
    ".hg/",
    ".svn/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    ".ipynb_checkpoints/",
    # Office, LibreOffice and editor lock and temp files
    "~$*",
    ".~lock.*#",
    "~*.tmp",
    # operating-system and sync-client clutter
    ".DS_Store",
    "._*",
    "Thumbs.db",
    "desktop.ini",
    "$RECYCLE.BIN/",
    ".Trash*/",
    ".Spotlight-V100/",
    ".fseventsd/",
    ".dropbox",
    ".dropbox.cache/",
    ".tmp.drivedownload/",
    ".tmp.driveupload/",
    # Eridi's own files
    ROOT_MARKER,
    IGNORE_FILE,
)

# Windows file attributes for files a sync client keeps only in the cloud.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_PLACEHOLDER_ATTRIBUTES = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_placeholder(st: os.stat_result, platform: str = sys.platform) -> bool:
    """True if a sync client keeps this file's content only in the cloud."""
    if platform == "win32":
        attributes = getattr(st, "st_file_attributes", 0)
        return bool(attributes & _PLACEHOLDER_ATTRIBUTES)
    if platform == "darwin":
        # iCloud, OneDrive and Google Drive (File Provider) leave dataless files that take no
        # disk blocks but report their full size.
        return st.st_size > 0 and getattr(st, "st_blocks", 1) == 0
    return False


def build_ignore_spec(root: Path, extra: Iterable[str] = ()) -> pathspec.GitIgnoreSpec:
    """Default patterns, then `extra`, then the root's `.eridiignore` (gitignore syntax)."""
    lines = [*DEFAULT_IGNORES, *extra]
    ignore_file = root / IGNORE_FILE
    try:
        lines += ignore_file.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Could not read %s, using default ignore rules: %s", ignore_file, exc)
    return pathspec.GitIgnoreSpec.from_lines(lines)


class LocalConnector:
    kind = "local"

    def __init__(
        self,
        root: Path,
        root_id: str,
        *,
        read_online_only_files: bool = False,
        ignore: Iterable[str] = (),
    ) -> None:
        self.root = root
        self._root_id = root_id
        self.read_online_only_files = read_online_only_files
        self._extra_ignores = tuple(ignore)

    def root_id(self) -> str:
        return self._root_id

    def _resolved_root(self) -> Path:
        try:
            return self.root.resolve(strict=True)
        except OSError:
            raise ConnectorError(
                f"The storage folder {self.root} can't be found. "
                "Reconnect the drive or update the folder's location."
            ) from None

    def list_files(self) -> Iterator[RemoteFile]:
        """Every file that isn't ignored: each folder's files, then its subfolders."""
        resolved_root = self._resolved_root()
        spec = build_ignore_spec(self.root, self._extra_ignores)

        def on_error(exc: OSError) -> None:
            log.warning("Skipped a folder that could not be read: %s (%s)", exc.filename, exc)

        for dirpath, dirnames, filenames in os.walk(self.root, onerror=on_error):
            directory = Path(dirpath)
            rel_dir = directory.relative_to(self.root).as_posix()
            prefix = "" if rel_dir == "." else f"{rel_dir}/"
            kept = []
            for name in sorted(dirnames):
                if (directory / name).is_symlink():
                    log.debug("Not following folder link %s", directory / name)
                    continue
                if not spec.match_file(f"{prefix}{name}/"):
                    kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                rel = f"{prefix}{name}"
                if spec.match_file(rel):
                    continue
                remote = self._describe(directory / name, rel, resolved_root)
                if remote is not None:
                    yield remote

    def _describe(self, path: Path, rel: str, resolved_root: Path) -> RemoteFile | None:
        try:
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(resolved_root):
                    log.info("Skipped a link that points outside the storage folder: %s", path)
                    return None
            st = path.stat()
        except OSError as exc:
            log.warning("Skipped a file that could not be read: %s (%s)", path, exc.strerror)
            return None
        if not stat_module.S_ISREG(st.st_mode):
            return None
        return RemoteFile(
            path=rel,
            size=st.st_size,
            modified=st.st_mtime,
            is_placeholder=is_placeholder(st),
        )

    def _full_path(self, path: str) -> Path:
        rel = normalize_relative(path)
        full = self.root.joinpath(*rel.split("/"))
        if not full.resolve().is_relative_to(self._resolved_root()):
            raise ConnectorError(f"{path} is outside the storage folder {self.root}.")
        return full

    def open_bytes(self, path: str) -> bytes:
        full = self._full_path(path)
        try:
            if not self.read_online_only_files and is_placeholder(full.stat()):
                raise PlaceholderNotReadError(
                    f"{path} is online-only. Make it available offline in your sync app, "
                    "or turn on reading online-only files (this downloads them)."
                )
            with open(full, "rb") as handle:  # noqa: PTH123 - read-only, explicit mode
                return handle.read()
        except OSError as exc:
            raise ConnectorError(f"Could not read {path}: {exc.strerror or exc}") from None

    def versions(self, path: str) -> list[RemoteVersion]:
        return []  # plain folders keep no history; Eridi's snapshots fill the gap

    def open_version(self, path: str, version_id: str) -> bytes:
        raise ConnectorError(f"{path}: local folders keep no earlier versions.")

    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]:
        """Files modified since the cursor (a timestamp), or all files when it is None.

        Removed files aren't reported here; the scanner finds them by comparing listings.
        """
        started = time.time()
        since = float(cursor) if cursor else None
        changes = [
            RemoteChange(f.path, "added" if since is None else "modified", f)
            for f in self.list_files()
            if since is None or f.modified > since
        ]
        return changes, repr(started)
