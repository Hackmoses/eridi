"""Small file-system helpers for the places Eridi writes: app data and the record folder."""

import contextlib
import os
import sys
import time
from pathlib import Path

_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY_S = 0.05


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry to disk after a rename (POSIX only; a no-op on Windows)."""
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # some file systems (network, FUSE sync clients) refuse; data is still written
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(
    target: Path, data: bytes, *, tmp_dir: Path | None = None, overwrite: bool = True
) -> None:
    """Write `data` to `target` so readers see either nothing or the whole file.

    The bytes go to a temporary file (in `tmp_dir`, default the target's folder), are flushed
    with fsync, then renamed into place. With `overwrite=False` an existing target raises
    `FileExistsError` and is left untouched.
    """
    tmp_dir = tmp_dir or target.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{target.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as handle:  # noqa: PTH123 - need the handle for fsync
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            _replace(tmp, target)
        else:
            _place_new(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    fsync_dir(target.parent)


def _place_new(source: Path, target: Path) -> None:
    """Move `source` to `target` atomically, failing if `target` exists."""
    exists = FileExistsError(f"{target} already exists and will not be overwritten.")
    if sys.platform == "win32":
        try:
            source.rename(target)  # Windows rename never replaces an existing file
        except FileExistsError:
            raise exists from None
        return
    try:
        os.link(source, target)  # atomic, and fails if the name is taken
    except FileExistsError:
        raise exists from None
    except OSError:
        # Some file systems (FAT, some network and sync mounts) have no hard links.
        if target.exists():
            raise exists from None
        _replace(source, target)


def _replace(source: Path, target: Path) -> None:
    # On Windows, sync clients and virus scanners briefly lock files; retry a little.
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S * (attempt + 1))
