"""Cloud drives as item trees: the part every cloud connector shares.

Cloud APIs report changes by item ID (a file's ID, its parent folder's ID and its name),
not by path. Connectors keep each drive's items as a tree and work out paths from it, so
a renamed folder moves everything under it without extra calls. The tree and the
provider's change cursor are kept between scans in a `DeltaCache` (the index, in the app).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import pathspec

from eridi.connectors.base import ConnectorError, RemoteChange, RemoteFile
from eridi.connectors.local import DEFAULT_IGNORES


@dataclass(frozen=True)
class CloudItem:
    id: str
    parent_id: str | None
    name: str
    is_folder: bool
    size: int = 0
    modified: float = 0.0
    is_root: bool = False
    mime: str = ""
    """The provider's content type, where the connector needs it (Google's native docs)."""


class DeltaCache(Protocol):
    """Where a drive's item tree and delta cursor are kept between scans."""

    def load(self, root_id: str) -> tuple[str | None, dict[str, CloudItem]]: ...

    def save(
        self, root_id: str, cursor: str, upserts: list[CloudItem], removed: list[str], *,
        reset: bool,
    ) -> None: ...  # fmt: skip


class MemoryDeltaCache:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str | None, dict[str, CloudItem]]] = {}

    def load(self, root_id: str) -> tuple[str | None, dict[str, CloudItem]]:
        cursor, items = self.data.get(root_id, (None, {}))
        return cursor, dict(items)

    def save(
        self, root_id: str, cursor: str, upserts: list[CloudItem], removed: list[str], *,
        reset: bool,
    ) -> None:  # fmt: skip
        _, items = (None, {}) if reset else self.load(root_id)
        for item_id in removed:
            items.pop(item_id, None)
        items.update({i.id: i for i in upserts})
        self.data[root_id] = (cursor, items)


def iso_timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def item_paths(items: dict[str, CloudItem]) -> dict[str, str]:
    """Item ID → path relative to the drive root, for every item that reaches the root."""
    out: dict[str, str] = {}
    missing = object()

    def path_of(item_id: str, depth: int = 0) -> str | None:
        known = out.get(item_id, missing)
        if known is not missing:
            return known  # type: ignore[return-value]
        item = items.get(item_id)
        if item is None or depth > 200:
            return None
        if item.is_root:
            out[item_id] = ""
            return ""
        parent = path_of(item.parent_id, depth + 1) if item.parent_id else None
        if parent is None:
            return None  # an orphan: its folder was deleted or not delivered yet
        path = f"{parent}/{item.name}" if parent else item.name
        out[item_id] = path
        return path

    for item_id in items:
        path_of(item_id)
    return {k: v for k, v in out.items() if v}


class TreeConnector:
    """Listing, paths and change reports for a connector that keeps an item tree.
    Subclasses implement `sync()` (bring the tree up to date) and reading."""

    kind = ""
    missing_message = "{path} isn't in this drive any more."

    def __init__(
        self, root_id: str, *, cache: DeltaCache | None = None, ignore: Iterable[str] = ()
    ) -> None:
        self._root_id = root_id
        self.cache: DeltaCache = cache or MemoryDeltaCache()
        self._ignore = pathspec.GitIgnoreSpec.from_lines([*DEFAULT_IGNORES, *ignore])
        self._items: dict[str, CloudItem] | None = None
        self._cursor: str | None = None
        self._by_path: dict[str, str] | None = None

    def root_id(self) -> str:
        return self._root_id

    def _load(self) -> dict[str, CloudItem]:
        if self._items is None:
            self._cursor, self._items = self.cache.load(self._root_id)
        return self._items

    def sync(self) -> list[RemoteChange]:
        raise NotImplementedError

    def _files(self, items: dict[str, CloudItem]) -> dict[str, str]:
        """Item ID → path, for the files a scan should see."""
        return {
            i: p for i, p in item_paths(items).items() if not items[i].is_folder and self._wanted(p)
        }

    def _changes(
        self, before: dict[str, str], items: dict[str, CloudItem], touched: set[str]
    ) -> list[RemoteChange]:
        after = self._files(items)
        changes = [
            RemoteChange(path, "removed")
            for item_id, path in before.items()
            if after.get(item_id) != path
        ]
        for item_id, path in after.items():
            remote = self._remote(items[item_id], path)
            if before.get(item_id) != path:
                changes.append(RemoteChange(path, "added", remote))
            elif item_id in touched:
                changes.append(RemoteChange(path, "modified", remote))
        return changes

    def _wanted(self, path: str) -> bool:
        return not self._ignore.match_file(path)

    @staticmethod
    def _remote(item: CloudItem, path: str) -> RemoteFile:
        return RemoteFile(path=path, size=item.size, modified=item.modified)

    def list_files(self) -> Iterator[RemoteFile]:
        self.sync()
        items = self._load()
        for item_id, path in sorted(self._files(items).items(), key=lambda kv: kv[1]):
            yield self._remote(items[item_id], path)

    def _item_id(self, path: str) -> str:
        if self._by_path is None:
            self._load()
            if self._cursor is None:
                self.sync()
            self._by_path = {p: i for i, p in item_paths(self._load()).items()}
        item_id = self._by_path.get(path)
        if item_id is None:
            raise ConnectorError(self.missing_message.format(path=path))
        return item_id
