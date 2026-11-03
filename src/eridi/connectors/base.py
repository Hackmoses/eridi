"""The interface every storage source implements.

Connectors are read-only: they list files and return bytes. The engine never touches a
storage source except through this protocol, so adding a source never changes the engine.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


class ConnectorError(Exception):
    """A storage source could not do what was asked. The message says why and what to do."""


class PlaceholderNotReadError(ConnectorError):
    """The file is online-only, and reading it would download it."""


@dataclass(frozen=True)
class RemoteFile:
    """A file as a storage source reports it. `path` is POSIX-style, relative to the root."""

    path: str
    size: int
    modified: float
    """Seconds since the Unix epoch, UTC."""
    content_sha256: str | None = None
    """Set when the source already knows it, so the engine can skip hashing."""
    is_placeholder: bool = False
    """An online-only file a sync client hasn't downloaded."""


@dataclass(frozen=True)
class RemoteVersion:
    """An earlier version of a file that the storage source keeps."""

    id: str
    modified: float
    size: int | None = None
    author: str | None = None


ChangeKind = Literal["added", "modified", "removed"]


@dataclass(frozen=True)
class RemoteChange:
    path: str
    kind: ChangeKind
    file: RemoteFile | None = None
    """The file's current state; None when it was removed."""


@runtime_checkable
class Connector(Protocol):
    kind: str
    """"local", "msgraph" or "gdrive"."""

    def root_id(self) -> str: ...

    def list_files(self) -> Iterator[RemoteFile]: ...

    def open_bytes(self, path: str) -> bytes:
        """The file's bytes, read-only."""
        ...

    def versions(self, path: str) -> list[RemoteVersion]:
        """Earlier versions, newest first; [] if the source keeps no history."""
        ...

    def open_version(self, path: str, version_id: str) -> bytes: ...

    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]:
        """Changes since `cursor` (all files when None) and the cursor for next time."""
        ...

    # From M11, only for storage the user allowed saving to:
    # def save(self, path: str, data: bytes) -> RemoteFile: ...
