"""M2.1: the connector protocol and its data types."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest

from eridi.connectors.base import Connector, RemoteChange, RemoteFile, RemoteVersion


class _Memory:
    """A minimal in-memory connector, to check the protocol is implementable."""

    kind = "memory"

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def root_id(self) -> str:
        return "root_MEMORY"

    def list_files(self) -> Iterator[RemoteFile]:
        for path, data in sorted(self.files.items()):
            yield RemoteFile(path, len(data), 0.0)

    def open_bytes(self, path: str) -> bytes:
        return self.files[path]

    def versions(self, path: str) -> list[RemoteVersion]:
        return []

    def open_version(self, path: str, version_id: str) -> bytes:
        raise KeyError(version_id)

    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]:
        return [RemoteChange(f.path, "added", f) for f in self.list_files()], "1"


def test_protocol_is_implementable() -> None:
    memory = _Memory({"b.md": b"bb", "a.md": b"a"})
    assert isinstance(memory, Connector)
    assert [f.path for f in memory.list_files()] == ["a.md", "b.md"]
    changes, cursor = memory.changes_since(None)
    assert [c.kind for c in changes] == ["added", "added"]
    assert cursor == "1"


def test_records_are_immutable() -> None:
    f = RemoteFile("a.md", 1, 0.0)
    assert not f.is_placeholder
    assert f.content_sha256 is None
    with pytest.raises(FrozenInstanceError):
        f.size = 2  # type: ignore[misc]
