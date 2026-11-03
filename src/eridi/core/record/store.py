"""RecordStore: the append-only record folder.

One JSON file per entry at `entries/YYYY/MM/<ULID>.json`, written atomically. Files that
don't match the entry name pattern (sync conflict copies, stray files) are never read as
entries; they are reported so `eridi doctor` can show them.
"""

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ulid import ULID

from eridi.core.fsutil import atomic_write_bytes
from eridi.core.ids import is_ulid, new_ulid
from eridi.core.models import Entry, utc_now
from eridi.core.record.schema import (
    RecordFormatError,
    UnsupportedSchemaError,
    parse_entry,
    serialize_entry,
)

log = logging.getLogger(__name__)

MARKER_NAME = "eridi-record.json"
RECORDSET_SCHEMA = "eridi.recordset/1"
ENTRIES_DIR = "entries"
TMP_DIR = ".tmp"
ENTRY_FILE_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\.json$")
NEWER_VERSION_WARNING = "Some record entries were written by a newer version. Update to see them."
# Files operating systems drop into folders; not worth reporting.
_OS_FILES = {"desktop.ini", "thumbs.db", "icon\r"}


class RecordFolderError(ValueError):
    """The folder is not a usable record folder."""


@dataclass(frozen=True)
class RecordProblem:
    path: Path
    reason: str


@dataclass
class RecordRead:
    """Everything one pass over the record folder found."""

    entries: list[Entry] = field(default_factory=list)
    problems: list[RecordProblem] = field(default_factory=list)
    newer_version: list[Path] = field(default_factory=list)


class RecordStore:
    """Read and append entries in a record folder created with `RecordStore.init`."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        marker = folder / MARKER_NAME
        try:
            data: Any = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RecordFolderError(
                f"{folder} is not a record folder (no {MARKER_NAME}). "
                "Choose an existing record folder or create one with `eridi init`."
            ) from None
        except (OSError, ValueError) as exc:
            raise RecordFolderError(f"Could not read {marker}: {exc}") from None
        if not isinstance(data, dict) or data.get("schema") != RECORDSET_SCHEMA:
            raise RecordFolderError(
                f"{marker} is not a recognized record marker "
                f"(expected schema {RECORDSET_SCHEMA!r}). It may be from a newer version."
            )
        self.id: str = str(data.get("id", ""))
        self.created_at: str = str(data.get("created_at", ""))

    @classmethod
    def init(cls, folder: Path) -> "RecordStore":
        """Create a record folder, or open it if it already is one."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ENTRIES_DIR).mkdir(exist_ok=True)
        marker = folder / MARKER_NAME
        if not marker.exists():
            body = {"schema": RECORDSET_SCHEMA, "id": new_ulid(), "created_at": utc_now()}
            text = json.dumps(body, sort_keys=True, indent=2) + "\n"
            # If another device created it first, use theirs.
            with contextlib.suppress(FileExistsError):
                atomic_write_bytes(marker, text.encode("utf-8"), overwrite=False)
        return cls(folder)

    @property
    def entries_dir(self) -> Path:
        return self.folder / ENTRIES_DIR

    def path_for(self, entry: Entry) -> Path:
        created = entry.created
        return (
            self.entries_dir / f"{created.year:04d}" / f"{created.month:02d}" / f"{entry.id}.json"
        )

    def append(self, entry: Entry) -> Path:
        """Write a new entry. Existing entry files are never overwritten."""
        text = serialize_entry(entry)
        parse_entry(json.loads(text))  # refuse to write anything a reader would reject
        target = self.path_for(entry)
        atomic_write_bytes(
            target,
            text.encode("utf-8"),
            tmp_dir=self.entries_dir / TMP_DIR,
            overwrite=False,
        )
        return target

    def _entry_files(self, result: RecordRead) -> Iterator[Path]:
        stack = [self.entries_dir]
        while stack:
            directory = stack.pop()
            try:
                children = list(directory.iterdir())
            except FileNotFoundError:
                continue
            except OSError as exc:
                result.problems.append(RecordProblem(directory, f"could not list folder: {exc}"))
                continue
            for child in children:
                name = child.name
                if child.is_dir():
                    if name != TMP_DIR and not name.startswith("."):
                        stack.append(child)
                elif ENTRY_FILE_RE.match(name):
                    yield child
                elif not name.startswith(".") and name.lower() not in _OS_FILES:
                    result.problems.append(
                        RecordProblem(
                            child,
                            "not an entry file name; possibly a sync conflict copy, ignored",
                        )
                    )

    def read(self, after: str | None = None) -> RecordRead:
        """Read every valid entry (only IDs greater than `after`, if given), sorted by ID."""
        result = RecordRead()
        for path in self._entry_files(result):
            if after is not None and path.stem <= after:
                continue
            entry = self._load(path, result)
            if entry is not None:
                result.entries.append(entry)
        result.entries.sort(key=lambda e: e.id)
        result.problems.sort(key=lambda p: str(p.path))
        if result.newer_version:
            log.warning(NEWER_VERSION_WARNING)
        if result.problems:
            log.info("Skipped %d unreadable record files.", len(result.problems))
        return result

    def _load(self, path: Path, result: RecordRead) -> Entry | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            result.problems.append(RecordProblem(path, f"could not read file: {exc}"))
            return None
        except ValueError as exc:
            result.problems.append(RecordProblem(path, f"invalid JSON: {exc}"))
            return None
        try:
            entry = parse_entry(data)
        except UnsupportedSchemaError:
            result.newer_version.append(path)
            return None
        except RecordFormatError as exc:
            result.problems.append(RecordProblem(path, f"invalid entry: {exc}"))
            return None
        if entry.id != path.stem:
            result.problems.append(
                RecordProblem(path, f"file name does not match the entry ID {entry.id}")
            )
            return None
        return entry

    def iter_entries(self, after: str | None = None) -> Iterator[Entry]:
        """Yield all valid entries sorted by ID. Problems are logged, not raised."""
        yield from self.read(after).entries

    def get(self, entry_id: str) -> Entry | None:
        """Return one entry by ID, or None if it isn't in the record."""
        if not is_ulid(entry_id):
            return None
        # The folder comes from created_at, which normally matches the ULID's own timestamp.
        moment = ULID.from_str(entry_id).datetime
        name = f"{entry_id}.json"
        guess = self.entries_dir / f"{moment.year:04d}" / f"{moment.month:02d}" / name
        candidates = [guess] if guess.is_file() else sorted(self.entries_dir.rglob(name))
        scratch = RecordRead()
        for path in candidates:
            if TMP_DIR in path.relative_to(self.entries_dir).parts:
                continue
            entry = self._load(path, scratch)
            if entry is not None:
                return entry
        return None
