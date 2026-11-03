"""Plain data types shared across the engine.

Record entries mirror `docs/RECORD_FORMAT.md` (schema `eridi.record/1`). Every record type
keeps the fields it doesn't know in `extra_unknown`, so an entry written by a newer version
survives being read and written by this one.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

RECORD_SCHEMA = "eridi.record/1"

EntryKind = Literal["note", "decision", "outcome", "link", "project", "tombstone"]
ENTRY_KINDS: tuple[str, ...] = ("note", "decision", "outcome", "link", "project", "tombstone")

AuthorKind = Literal["human", "agent"]
AUTHOR_KINDS: tuple[str, ...] = ("human", "agent")

Outcome = Literal["worked", "partial", "failed", "unknown"]
OUTCOMES: tuple[str, ...] = ("worked", "partial", "failed", "unknown")

LinkRelation = Literal["caused", "replaces", "relates"]
LINK_RELATIONS: tuple[str, ...] = ("caused", "replaces", "relates")

JSON = dict[str, Any]


@dataclass(frozen=True)
class Author:
    name: str
    kind: str = "human"
    email: str | None = None
    extra_unknown: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class Subject:
    """A file an entry is about. `path` is POSIX-style, relative to the root."""

    root_id: str
    path: str
    root_label: str | None = None
    content_sha256: str | None = None
    text_sha256: str | None = None
    extra_unknown: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class EntryChange:
    """The detected change between two text versions that an entry explains."""

    from_text_sha256: str | None
    to_text_sha256: str
    summary: str | None = None
    extra_unknown: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class Link:
    """One connection in a `link` entry. `from`/`to` are an entry ID or a subject object."""

    from_: str | JSON
    to: str | JSON
    relation: str
    extra_unknown: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectFolder:
    """A folder that belongs to a project: a root, and a path prefix inside it ("" = all)."""

    root_id: str
    path: str = ""
    extra_unknown: JSON = field(default_factory=dict, hash=False)

    def contains(self, root_id: str, path: str) -> bool:
        if root_id != self.root_id:
            return False
        return not self.path or path == self.path or path.startswith(self.path + "/")


@dataclass(frozen=True)
class ProjectRef:
    """The project a `project` entry creates or renames.

    `folders` is the project's complete folder list as of this entry; `None` means the entry
    doesn't say (it keeps the folders from the project's previous entry).
    """

    id: str
    name: str
    home_root_id: str | None = None
    folders: tuple[ProjectFolder, ...] | None = None
    labels: tuple[str, ...] | None = None
    """Data-safety labels (e.g. "patient-data"); `None` keeps the previous entry's."""
    extra_unknown: JSON = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    created_at: str
    author: Author
    device_id: str
    text: str = ""
    subjects: tuple[Subject, ...] = ()
    project_id: str | None = None
    change: EntryChange | None = None
    outcome: str | None = None
    tags: tuple[str, ...] = ()
    supersedes: str | None = None
    target_id: str | None = None
    links: tuple[Link, ...] | None = None
    project: ProjectRef | None = None
    extra: JSON = field(default_factory=dict)
    schema: str = RECORD_SCHEMA
    extra_unknown: JSON = field(default_factory=dict)

    @property
    def created(self) -> datetime:
        """`created_at` as an aware UTC datetime."""
        return parse_timestamp(self.created_at)

    @property
    def is_supported_kind(self) -> bool:
        return self.kind in ENTRY_KINDS


def format_timestamp(moment: datetime) -> str:
    """UTC ISO 8601 with milliseconds and `Z`, e.g. `2026-11-03T14:05:22.481Z`."""
    if moment.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_timestamp(value: str) -> datetime:
    """Parse a record timestamp. Raises ValueError unless it is UTC ISO 8601."""
    if not value.endswith("Z"):
        raise ValueError(f"{value!r} is not a UTC timestamp ending in 'Z'.")
    moment = datetime.fromisoformat(value[:-1] + "+00:00")
    return moment.astimezone(UTC)


def utc_now() -> str:
    return format_timestamp(datetime.now(UTC))


DATA_LABELS: dict[str, str] = {"patient-data": "Contains patient data"}
"""Data-safety labels a project can carry. Saving a labeled project's files is allowed only
to storage approved for every one of its labels."""


@dataclass(frozen=True)
class Root:
    """A connected storage location. `location` is a local path or a provider's drive ID."""

    id: str
    label: str
    location: str
    kind: str = "local"
    search: bool = True
    save: bool = False
    is_home_default: bool = False
    account: str = ""
    """For cloud storage: the signed-in account it is read with (empty for local folders)."""
    approved_labels: tuple[str, ...] = ()
    """Data-safety labels this storage is approved for (e.g. "patient-data")."""


@dataclass(frozen=True)
class FileInfo:
    """A file as the index knows it. `path` is POSIX-style, relative to its root."""

    root_id: str
    path: str
    size: int
    modified: float
    ext: str
    content_sha256: str | None = None
    text_sha256: str | None = None
    status: str = "present"
    """"present" or "missing" (gone from storage; its history is kept)."""
    placeholder: bool = False
    title: str | None = None
    extract_status: str | None = None
    last_seen: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Project:
    """A project's current state, from its latest `project` entry."""

    id: str
    name: str
    home_root_id: str | None = None
    folders: tuple[ProjectFolder, ...] = ()
    labels: tuple[str, ...] = ()
