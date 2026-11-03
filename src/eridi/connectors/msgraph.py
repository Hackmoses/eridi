"""Microsoft OneDrive and SharePoint document libraries, through Microsoft Graph.

Read-only, except `save()` (M11): it uploads files the user saves through Eridi, only to
storage with Save on, with a separate write token, and never replaces a file unless asked.

A drive's file list is kept up to date with Graph's delta query (`/drives/{id}/root/delta`):
the first listing pages through every item once, later ones ask only for what changed
since the saved cursor. Items are kept as a tree (ID, parent, name), because delta
responses for work and school drives don't include paths; a renamed folder then moves
everything under it without any extra calls.

Files are only downloaded when the scanner asks for their bytes (new or changed files).
Version history comes from `/items/{id}/versions`.
"""

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from eridi.connectors.base import (
    ConnectorError,
    NameTakenInStorageError,
    RemoteChange,
    RemoteFile,
    RemoteVersion,
)
from eridi.connectors.http import MAX_WAIT, TIMEOUT, ApiClient, ApiError, TokenProvider
from eridi.connectors.tree import (
    CloudItem,
    DeltaCache,
    MemoryDeltaCache,
    TreeConnector,
    iso_timestamp,
    item_paths,
)

__all__ = [
    "MAX_WAIT",
    "TIMEOUT",
    "CloudItem",
    "DeltaCache",
    "MemoryDeltaCache",
    "TokenProvider",
    "item_paths",
]

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT_ID_PREFIX = "ms:"
"""Storage IDs for Microsoft drives are `ms:<driveId>` (see RECORD_FORMAT.md, "Roots")."""
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
"""Graph takes files up to 4 MB in one request; larger ones go through an upload session."""
UPLOAD_CHUNK = 10 * 320 * 1024
"""Upload-session chunks must be multiples of 320 KiB."""
SAVE_EVERY_PAGES = 20
"""While listing a large drive, save progress this often, so an interrupted first scan
picks up where it stopped."""


def root_id_for(drive_id: str) -> str:
    return ROOT_ID_PREFIX + drive_id


class GraphError(ApiError):
    pass


class ResyncRequiredError(GraphError):
    """The saved delta cursor expired; the drive must be listed again from the start."""


# --- HTTP ---


class GraphClient(ApiClient):
    """Plain HTTPS to Microsoft Graph with a bearer token."""

    base = GRAPH
    provider = "Microsoft"

    def error_for(self, response: httpx.Response) -> GraphError:
        return _error_for(response)

    def unreachable(self, exc: Exception) -> GraphError:
        return GraphError(
            f"Couldn't reach Microsoft ({type(exc).__name__}). Check the internet "
            "connection; Eridi will try again at the next scan."
        )

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        data = self.request(path_or_url, params).json()
        if not isinstance(data, dict):
            raise GraphError("Microsoft returned something unexpected. Try again later.")
        return data

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
    if status == 429 or status >= 500:
        return GraphError(
            f"Microsoft is busy (HTTP {status}) and asked Eridi to slow down. "
            "The next scan will carry on.", status, code,
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
        modified=iso_timestamp(data.get("lastModifiedDateTime")),
        is_root="root" in data,
    )


# --- the connector ---


class MsGraphConnector(TreeConnector):
    kind = "msgraph"
    missing_message = "{path} isn't in this Microsoft drive any more."

    def __init__(
        self,
        root_id: str,
        drive_id: str,
        client: GraphClient,
        *,
        cache: DeltaCache | None = None,
        ignore: Iterable[str] = (),
        writer: GraphClient | None = None,
    ) -> None:
        """`writer` is a client with a write token; without it `save()` refuses."""
        super().__init__(root_id, cache=cache, ignore=ignore)
        self.writer = writer
        self.drive_id = drive_id
        self.client = client

    @property
    def _base(self) -> str:
        return f"/drives/{quote(self.drive_id, safe='!')}"

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
        """Bring the item tree up to date. Returns what changed, by path.

        Progress is saved every `SAVE_EVERY_PAGES` pages, so an interrupted listing resumes
        from where it stopped. If Microsoft says the saved cursor is too old, the drive is
        listed again from the start."""
        items = self._load()
        before = self._files(items)
        try:
            touched = self._run_delta(items)
        except ResyncRequiredError:
            if self._cursor is None:
                raise
            log.info("Listing %s again from the start (Microsoft asked to resync).", self.drive_id)
            self._cursor = None
            touched = self._run_delta(items)
        self._by_path = None
        return self._changes(before, items, touched)

    def _run_delta(self, items: dict[str, CloudItem]) -> set[str]:
        reset = self._cursor is None
        url = self._cursor or f"{self._base}/root/delta"
        upserts: list[CloudItem] = []
        removed: list[str] = []
        touched: set[str] = set()
        unsaved = 0
        if reset:
            items.clear()  # in memory only; the saved tree is replaced at the first save
        for page in self.client.pages(url):
            for data in page.get("value", []):
                item = None if "deleted" in data else _item(data)
                if item is None:  # deleted, or became something we don't read
                    removed.append(str(data["id"]))
                    items.pop(str(data["id"]), None)
                else:
                    upserts.append(item)
                    items[item.id] = item
                    touched.add(item.id)
            unsaved += 1
            done = page.get("@odata.deltaLink")
            link = done or page.get("@odata.nextLink")
            if link and (done or unsaved >= SAVE_EVERY_PAGES):
                self.cache.save(self._root_id, str(link), upserts, removed, reset=reset)
                self._cursor = str(link)
                reset, unsaved, upserts, removed = False, 0, [], []
            if done:
                return touched
        raise GraphError("Microsoft didn't finish listing the drive. Try again later.")

    def _by_path_url(self, path: str) -> str:
        return f"{self._base}/root:/{quote(path, safe='/')}:"

    def open_bytes(self, path: str) -> bytes:
        try:
            item_id = self._item_id(path)
        except ConnectorError as missing:  # e.g. just saved, not listed yet: try by path
            try:
                return self.client.get_bytes(f"{self._by_path_url(path)}/content")
            except GraphError as exc:
                if exc.status == 404:
                    raise missing from None
                raise
        return self.client.get_bytes(f"{self._base}/items/{item_id}/content")

    def exists(self, path: str) -> bool:
        try:
            self.client.get(self._by_path_url(path))
        except GraphError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def save(self, path: str, data: bytes, *, overwrite: bool = False) -> RemoteFile:
        """Upload a file. Without `overwrite`, an existing file is left alone and
        `NameTakenInStorageError` is raised; with it, Microsoft keeps the old one as a
        version."""
        if self.writer is None:
            raise ConnectorError("Saving to this Microsoft storage is off.")
        behavior = "replace" if overwrite else "fail"
        target = self._by_path_url(path)
        try:
            if len(data) <= SIMPLE_UPLOAD_MAX:
                response = self.writer.send(
                    "PUT", f"{target}/content", content=data,
                    params={"@microsoft.graph.conflictBehavior": behavior},
                    headers={"Content-Type": "application/octet-stream"},
                )  # fmt: skip
            else:
                response = self._upload_session(target, data, behavior)
        except GraphError as exc:
            if exc.status == 409 or exc.code == "nameAlreadyExists":
                raise NameTakenInStorageError(f"{path} already exists in Microsoft.") from None
            raise
        item = response.json()
        self._by_path = None
        return RemoteFile(
            path=path,
            size=int(item.get("size") or len(data)),
            modified=iso_timestamp(item.get("lastModifiedDateTime")),
        )

    def _upload_session(self, target: str, data: bytes, behavior: str) -> httpx.Response:
        assert self.writer is not None  # noqa: S101 - checked by save()
        session = self.writer.send(
            "POST", f"{target}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": behavior}},
        ).json()  # fmt: skip
        url = str(session.get("uploadUrl") or "")
        if not url.startswith("https://"):
            raise GraphError("Microsoft didn't start the upload. Try again later.")
        response: httpx.Response | None = None
        total = len(data)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = data[start : start + UPLOAD_CHUNK]
            response = self.writer.send(
                "PUT", url, content=chunk, auth=False,
                headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"},
            )  # fmt: skip
        assert response is not None  # noqa: S101 - data is over 4 MB
        return response

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
                    modified=iso_timestamp(v.get("lastModifiedDateTime")),
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
