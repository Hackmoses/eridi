"""Microsoft OneDrive and SharePoint document libraries, through Microsoft Graph (read-only).

A drive's file list is kept up to date with Graph's delta query (`/drives/{id}/root/delta`):
the first listing pages through every item once, later ones ask only for what changed
since the saved cursor. Items are kept as a tree (ID, parent, name), because delta
responses for work and school drives don't include paths; a renamed folder then moves
everything under it without any extra calls.

Files are only downloaded when the scanner asks for their bytes (new or changed files).
Version history comes from `/items/{id}/versions`.
"""

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import pathspec

from eridi.connectors.base import ConnectorError, RemoteChange, RemoteFile, RemoteVersion
from eridi.connectors.local import DEFAULT_IGNORES

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT_ID_PREFIX = "ms:"
"""Storage IDs for Microsoft drives are `ms:<driveId>` (see RECORD_FORMAT.md, "Roots")."""
TIMEOUT = httpx.Timeout(30.0, read=120.0)


def root_id_for(drive_id: str) -> str:
    return ROOT_ID_PREFIX + drive_id


class GraphError(ConnectorError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ResyncRequiredError(GraphError):
    """The saved delta cursor expired; the drive must be listed again from the start."""


# --- HTTP ---

TokenProvider = Callable[[], str]


class GraphClient:
    """Plain HTTPS to Microsoft Graph with a bearer token."""

    def __init__(self, token: TokenProvider, *, http: httpx.Client | None = None) -> None:
        self._token = token
        self.http = http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("A Graph client can't be sent to another process.")

    def _url(self, path_or_url: str) -> str:
        return path_or_url if path_or_url.startswith("https://") else GRAPH + path_or_url

    def request(self, path_or_url: str, params: dict[str, str] | None = None) -> httpx.Response:
        url = self._url(path_or_url)
        try:
            response = self.http.get(
                url, params=params, headers={"Authorization": f"Bearer {self._token()}"}
            )
        except httpx.TransportError as exc:
            raise GraphError(
                f"Couldn't reach Microsoft ({type(exc).__name__}). Check the internet "
                "connection; Eridi will try again at the next scan."
            ) from None
        if response.status_code >= 400:
            raise _error_for(response)
        return response

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        data = self.request(path_or_url, params).json()
        if not isinstance(data, dict):
            raise GraphError("Microsoft returned something unexpected. Try again later.")
        return data

    def get_bytes(self, path_or_url: str) -> bytes:
        return self.request(path_or_url).content

    def pages(
        self, path_or_url: str, params: dict[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Every page of a collection, following `@odata.nextLink`."""
        page = self.get(path_or_url, params)
        while True:
            yield page
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return
            page = self.get(str(next_link))

    def items(
        self, path_or_url: str, params: dict[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        for page in self.pages(path_or_url, params):
            yield from page.get("value", [])


def _error_for(response: httpx.Response) -> GraphError:
    code = message = None
    try:
        body = response.json().get("error", {})
        code, message = body.get("code"), body.get("message")
    except (ValueError, AttributeError):
        pass
    status = response.status_code
    if status == 410 or code in ("resyncRequired", "syncStateNotFound", "syncStateInvalid"):
        return ResyncRequiredError("The drive must be listed again.", status, code)
    if status == 401:
        return GraphError(
            "Microsoft didn't accept Eridi's sign-in. Sign in again with "
            "`eridi storage add-microsoft`.", status, code,
        )  # fmt: skip
    if status == 403:
        return GraphError(
            "Microsoft refused access. Your organization may need to approve Eridi "
            "(see docs/IT_APPROVAL.md), or you no longer have access to this library.",
            status, code,
        )  # fmt: skip
    if status == 404:
        return GraphError("Not found in Microsoft storage (moved or deleted?).", status, code)
    detail = f": {message}" if message else ""
    return GraphError(f"Microsoft Graph error {status}{detail}", status, code)


# --- drives ---


@dataclass(frozen=True)
class DriveInfo:
    id: str
    name: str
    kind: str
    """"personal", "business" (OneDrive) or "documentLibrary" (SharePoint)."""
    site: str | None = None
    """The SharePoint site's name, for libraries."""
    web_url: str | None = None

    @property
    def label(self) -> str:
        if self.site:
            return (
                self.site
                if self.name in ("Documents", "Shared Documents")
                else (f"{self.site} – {self.name}")
            )
        return self.name


def _drive(data: dict[str, Any], site: str | None = None) -> DriveInfo:
    kind = str(data.get("driveType", ""))
    name = str(data.get("name") or ("OneDrive" if kind in ("personal", "business") else "Drive"))
    if kind in ("personal", "business") and name in ("OneDrive", "Documents"):
        name = "OneDrive"
    return DriveInfo(str(data["id"]), name, kind, site, data.get("webUrl"))


def list_drives(client: GraphClient) -> list[DriveInfo]:
    """The user's OneDrive, then the document libraries of SharePoint sites they follow."""
    drives = [_drive(client.get("/me/drive"))]
    for site in client.items("/me/followedSites"):
        site_name = str(site.get("displayName") or site.get("name") or "SharePoint")
        try:
            libraries = list(client.items(f"/sites/{site['id']}/drives"))
        except GraphError as exc:
            log.warning("Can't list libraries of %s: %s", site_name, exc)
            continue
        drives += [_drive(d, site_name) for d in libraries]
    return drives


# --- the item tree and its cache ---


@dataclass(frozen=True)
class CloudItem:
    id: str
    parent_id: str | None
    name: str
    is_folder: bool
    size: int = 0
    modified: float = 0.0
    is_root: bool = False


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


def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _item(data: dict[str, Any]) -> CloudItem | None:
    """A delta or listing item; None for things that aren't files or folders we can read
    (OneNote notebooks and other packages, items shared from other drives)."""
    if "package" in data or "remoteItem" in data:
        return None
    parent = data.get("parentReference") or {}
    return CloudItem(
        id=str(data["id"]),
        parent_id=parent.get("id"),
        name=str(data.get("name", "")),
        is_folder="folder" in data or "root" in data,
        size=int(data.get("size") or 0),
        modified=_timestamp(data.get("lastModifiedDateTime")),
        is_root="root" in data,
    )


def _paths(items: dict[str, CloudItem]) -> dict[str, str]:
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


# --- the connector ---


class MsGraphConnector:
    kind = "msgraph"

    def __init__(
        self,
        root_id: str,
        drive_id: str,
        client: GraphClient,
        *,
        cache: DeltaCache | None = None,
        ignore: Iterable[str] = (),
    ) -> None:
        self._root_id = root_id
        self.drive_id = drive_id
        self.client = client
        self.cache: DeltaCache = cache or MemoryDeltaCache()
        self._ignore = pathspec.GitIgnoreSpec.from_lines([*DEFAULT_IGNORES, *ignore])
        self._items: dict[str, CloudItem] | None = None
        self._cursor: str | None = None
        self._by_path: dict[str, str] | None = None

    def root_id(self) -> str:
        return self._root_id

    @property
    def _base(self) -> str:
        return f"/drives/{quote(self.drive_id, safe='!')}"

    def _load(self) -> dict[str, CloudItem]:
        if self._items is None:
            self._cursor, self._items = self.cache.load(self._root_id)
        return self._items

    def _delta(self, cursor: str | None) -> tuple[list[CloudItem], list[str], str]:
        """Changed and deleted item IDs since `cursor` (everything when None)."""
        upserts: list[CloudItem] = []
        removed: list[str] = []
        url = cursor or f"{self._base}/root/delta"
        cursor_out: str | None = None
        for page in self.client.pages(url):
            for data in page.get("value", []):
                if "deleted" in data:
                    removed.append(str(data["id"]))
                    continue
                item = _item(data)
                if item is None:
                    removed.append(str(data["id"]))  # e.g. a folder that became a package
                else:
                    upserts.append(item)
            cursor_out = page.get("@odata.deltaLink") or cursor_out
        if not cursor_out:
            raise GraphError("Microsoft didn't finish listing the drive. Try again later.")
        return upserts, removed, str(cursor_out)

    def sync(self) -> list[RemoteChange]:
        """Bring the item tree up to date. Returns what changed, by path."""
        items = self._load()
        before = self._files(items)
        reset = self._cursor is None
        upserts, removed, cursor = self._delta(self._cursor)
        if reset:
            items.clear()
        for item_id in removed:
            items.pop(item_id, None)
        items.update({i.id: i for i in upserts})
        self.cache.save(self._root_id, cursor, upserts, removed, reset=reset)
        self._cursor = cursor
        self._by_path = None
        return self._changes(before, items, {i.id for i in upserts})

    def _files(self, items: dict[str, CloudItem]) -> dict[str, str]:
        """Item ID → path, for the files a scan should see."""
        return {
            i: p for i, p in _paths(items).items() if not items[i].is_folder and self._wanted(p)
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
            self._by_path = {p: i for i, p in _paths(self._load()).items()}
        item_id = self._by_path.get(path)
        if item_id is None:
            raise ConnectorError(f"{path} isn't in this Microsoft drive any more.")
        return item_id

    def open_bytes(self, path: str) -> bytes:
        return self.client.get_bytes(f"{self._base}/items/{self._item_id(path)}/content")

    def versions(self, path: str) -> list[RemoteVersion]:
        """Earlier versions Microsoft keeps, newest first (the current one is left out)."""
        out: list[RemoteVersion] = []
        for i, v in enumerate(
            self.client.items(f"{self._base}/items/{self._item_id(path)}/versions")
        ):
            if i == 0:
                continue  # Graph lists the current version first
            user = ((v.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")
            size = v.get("size")
            out.append(
                RemoteVersion(
                    id=str(v["id"]),
                    modified=_timestamp(v.get("lastModifiedDateTime")),
                    size=int(size) if size is not None else None,
                    author=str(user) if user else None,
                )
            )
        return sorted(out, key=lambda v: -v.modified)

    def open_version(self, path: str, version_id: str) -> bytes:
        item_id = self._item_id(path)
        return self.client.get_bytes(
            f"{self._base}/items/{item_id}/versions/{quote(version_id, safe='')}/content"
        )

    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]:
        """Changes since `cursor` (every file when None), without touching the saved tree."""
        if cursor is None:
            items: dict[str, CloudItem] = {}
        else:
            items = dict(self._load())
        before = self._files(items)
        upserts, removed, new_cursor = self._delta(cursor)
        for item_id in removed:
            items.pop(item_id, None)
        items.update({i.id: i for i in upserts})
        return self._changes(before, items, {i.id for i in upserts}), new_cursor
