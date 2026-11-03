"""File-system locations and path normalization.

Paths stored in the record are POSIX-style and relative to a root, so the same file is
identified the same way on macOS, Windows and Linux, and on computers where a synced folder
lives at different absolute paths.
"""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from platformdirs import user_data_path

APP_NAME = "Eridi"
HOME_ENV = "ERIDI_HOME"


def app_home() -> Path:
    """Directory for config, index, snapshots and logs.

    Set `ERIDI_HOME` to override (tests use this to stay isolated).
    """
    override = os.environ.get(HOME_ENV)
    home = Path(override) if override else user_data_path(APP_NAME, appauthor=False)
    home.mkdir(parents=True, exist_ok=True)
    return home


class PathOutsideRootError(ValueError):
    """Raised when a path does not live under the root it is being made relative to."""


def to_posix_relative(root: Path, path: Path) -> str:
    """Return `path` relative to `root` as a POSIX string, e.g. `Protocols/WB/v7.docx`.

    Both paths are resolved first, so `..` segments and symlinks can't escape the root.
    """
    root_resolved = root.resolve()
    path_resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        relative = path_resolved.relative_to(root_resolved)
    except ValueError:
        raise PathOutsideRootError(
            f"{path} is outside the storage folder {root}. "
            "Only files inside a connected folder can be tracked."
        ) from None
    if relative == Path():
        raise PathOutsideRootError(f"{path} is the storage folder itself, not a file inside it.")
    return relative.as_posix()


def normalize_relative(value: str) -> str:
    """Normalize a relative path string from any OS to POSIX form.

    Accepts `Protocols\\WB\\v7.docx` or `Protocols/WB/v7.docx`. Rejects absolute paths and
    `..` segments, which would let a stored path point outside its root.
    """
    if not value or value.strip() != value:
        raise ValueError(f"{value!r} is not a valid relative path.")
    windows = PureWindowsPath(value)
    if windows.drive or windows.root or value.startswith("/"):
        raise ValueError(f"{value!r} is an absolute path. Stored paths must be relative.")
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"{value!r} is not a valid relative path.")
    if ".." in parts:
        raise ValueError(f"{value!r} contains '..'. Stored paths must stay inside their root.")
    return str(PurePosixPath(*parts))


def from_posix_relative(root: Path, value: str) -> Path:
    """Turn a stored POSIX relative path back into a native path under `root`."""
    return root.joinpath(*normalize_relative(value).split("/"))
