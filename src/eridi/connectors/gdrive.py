"""Google Drive: My Drive and shared drives, through the Drive API v3 (read-only).

The first listing pages through every file once (`files.list`); later scans ask only for
what changed since the saved page token (the Changes API). Like Microsoft's, items are kept
as a tree (ID, parent, name) and paths are worked out from it.

Native Google documents have no bytes of their own. They are listed under the names
Google's Drive for desktop app gives them (`Plan.gdoc`, `Budget.gsheet`, `Talk.gslides`),
so paths match a synced folder, and read by exporting: Docs and Slides as plain text,
Sheets as CSV (the first sheet). Revisions come from `files/{id}/revisions`.
"""

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from eridi.connectors.base import ConnectorError, RemoteChange, RemoteFile, RemoteVersion
from eridi.connectors.http import ApiClient, ApiError
from eridi.connectors.tree import (
    CloudItem,
    DeltaCache,
    TreeConnector,
    iso_timestamp,
)

log = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
ROOT_ID_PREFIX = "gd:"
"""Storage IDs for Google drives are `gd:<driveId>` (see RECORD_FORMAT.md, "Roots")."""
MY_DRIVE = "my-drive:"
SHARED_DRIVE = "shared-drive:"
"""A Google storage's `location` says which kind of drive it is, then its ID."""
SAVE_EVERY_PAGES = 20
FOLDER = "application/vnd.google-apps.folder"
NATIVE: dict[str, tuple[str, str | None]] = {
    "application/vnd.google-apps.document": (".gdoc", "text/plain"),
    "application/vnd.google-apps.spreadsheet": (".gsheet", "text/csv"),
    "application/vnd.google-apps.presentation": (".gslides", "text/plain"),
    "application/vnd.google-apps.drawing": (".gdraw", None),
    "application/vnd.google-apps.form": (".gform", None),
}
"""Google's own document types: the name suffix Drive for desktop uses, and what to export
them as (None: listed by name only)."""
FILE_FIELDS = "id,name,mimeType,parents,size,modifiedTime,trashed"
RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


def root_id_for(drive_id: str) -> str:
    return ROOT_ID_PREFIX + drive_id


def stub_doc_id(data: bytes) -> str | None:
    """The document ID in a `.gdoc`/`.gsheet`/`.gslides` shortcut file from a synced folder."""
    try:
        stub = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(stub, dict):
        return None
    for key in ("doc_id", "resource_id"):
        value = stub.get(key)
        if isinstance(value, str) and value:
            return value.split(":", 1)[-1]  # resource_id looks like "document:<id>"
    url = stub.get("url")
    if isinstance(url, str) and "/d/" in url:
        return url.split("/d/", 1)[1].split("/", 1)[0] or None
    return None


class DriveError(ApiError):
    pass


class ResyncRequiredError(DriveError):
    """The saved page token can't be used any more; the drive must be listed again."""


def _reason(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        error = response.json().get("error", {})
        errors = error.get("errors") or [{}]
        return errors[0].get("reason"), error.get("message")
    except (ValueError, AttributeError, IndexError):
        return None, None


class DriveClient(ApiClient):
    """Plain HTTPS to the Google Drive API with a bearer token."""

    base = DRIVE_API
    provider = "Google"

    def retryable(self, response: httpx.Response) -> bool:
        if response.status_code == 403:  # Google reports some throttling as 403
            return _reason(response)[0] in RATE_REASONS
        return super().retryable(response)

    def error_for(self, response: httpx.Response) -> DriveError:
        status = response.status_code
        reason, message = _reason(response)
        if status == 401:
            return DriveError(
                "Google didn't accept Eridi's sign-in. Sign in again with "
                "`eridi storage add-google`.", status, reason,
            )  # fmt: skip
        if status == 403 and reason == "exportSizeLimitExceeded":
            return DriveError(
                "Too large for Google to export as text (10 MB at most).", status, reason
            )
        if status == 403 and reason not in RATE_REASONS:
            return DriveError(
                "Google refused access. You may no longer have access to this drive or file.",
                status, reason,
            )  # fmt: skip
        if status == 429 or status >= 500 or reason in RATE_REASONS:
            return DriveError(
                f"Google is busy (HTTP {status}) and asked Eridi to slow down. "
                "The next scan will carry on.", status, reason,
            )  # fmt: skip
        if status == 404:
            return DriveError("Not found in Google Drive (moved or deleted?).", status, reason)
        detail = f": {message}" if message else ""
        return DriveError(f"Google Drive error {status}{detail}", status, reason)

    def unreachable(self, exc: Exception) -> DriveError:
        return DriveError(
            f"Couldn't reach Google ({type(exc).__name__}). Check the internet connection; "
            "Eridi will try again at the next scan."
        )

    def pages(self, path: str, params: dict[str, str], token: str | None = None) -> Iterator[Any]:
        """Every page of a list, following `nextPageToken`; starts at `token` if given."""
        while True:
            page = self.get(path, {**params, **({"pageToken": token} if token else {})})
            yield page
            token = page.get("nextPageToken")
            if not token:
                return


# --- drives and the signed-in user ---


@dataclass(frozen=True)
class DriveInfo:
    id: str
    name: str
    kind: str
    """"my_drive" or "shared_drive"."""

    @property
    def label(self) -> str:
        return self.name

    @property
    def location(self) -> str:
        return (MY_DRIVE if self.kind == "my_drive" else SHARED_DRIVE) + self.id


def whoami(client: DriveClient) -> tuple[str, str]:
    """(Google's permission ID for the user, their email)."""
    user = client.get("/about", {"fields": "user(permissionId,emailAddress)"}).get("user", {})
    return str(user.get("permissionId", "")), str(user.get("emailAddress", ""))


def list_drives(client: DriveClient) -> list[DriveInfo]:
    """My Drive, then the shared drives the user is a member of."""
    root = client.get("/files/root", {"fields": "id"})
    drives = [DriveInfo(str(root["id"]), "My Drive", "my_drive")]
    params = {"pageSize": "100", "fields": "nextPageToken,drives(id,name)"}
    for page in client.pages("/drives", params):
        for d in page.get("drives", []):
            drives.append(DriveInfo(str(d["id"]), str(d.get("name") or d["id"]), "shared_drive"))
    return drives


# --- the connector ---


def _item(data: dict[str, Any]) -> CloudItem | None:
    """A Drive file or folder; None for what we don't list (trashed files, shortcuts,
    Google types with nothing to read, files with no parent in this drive)."""
    if data.get("trashed"):
        return None
    mime = str(data.get("mimeType") or "")
    name = str(data.get("name") or "").replace("/", "_")  # Drive allows "/" in names
    if mime.startswith("application/vnd.google-apps.") and mime != FOLDER:
        if mime not in NATIVE:
            return None  # shortcuts, sites, maps, scripts…
        name += NATIVE[mime][0]
    parents = data.get("parents") or []
    return CloudItem(
        id=str(data["id"]),
        parent_id=str(parents[0]) if parents else None,
        name=name,
        is_folder=mime == FOLDER,
        size=int(data.get("size") or 0),
        modified=iso_timestamp(data.get("modifiedTime")),
        mime=mime,
    )


class GDriveConnector(TreeConnector):
    kind = "gdrive"
    missing_message = "{path} isn't in this Google drive any more."

    def __init__(
        self,
        root_id: str,
        location: str,
        client: DriveClient,
        *,
        cache: DeltaCache | None = None,
        ignore: Iterable[str] = (),
    ) -> None:
        """`location` is `my-drive:<root folder ID>` or `shared-drive:<drive ID>`."""
        super().__init__(root_id, cache=cache, ignore=ignore)
        self.shared = location.startswith(SHARED_DRIVE)
        self.drive_id = location.split(":", 1)[1] if ":" in location else location
        self.client = client

    # --- listing ---

    def _corpus(self) -> dict[str, str]:
        if self.shared:
            return {"driveId": self.drive_id, "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true"}  # fmt: skip
        return {}

    def _list_params(self) -> dict[str, str]:
        corpus = {"corpora": "drive"} if self.shared else {"corpora": "user", "spaces": "drive"}
        return {
            **corpus, **self._corpus(), "q": "trashed = false", "pageSize": "1000",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
        }  # fmt: skip

    def _change_params(self) -> dict[str, str]:
        mine = {} if self.shared else {"restrictToMyDrive": "true", "spaces": "drive"}
        return {
            **mine, **self._corpus(), "pageSize": "1000",
            "fields": "nextPageToken,newStartPageToken,"
            f"changes(fileId,removed,file({FILE_FIELDS}))",
        }  # fmt: skip

    def _start_token(self) -> str:
        params = {"driveId": self.drive_id, "supportsAllDrives": "true"} if self.shared else {}
        return str(self.client.get("/changes/startPageToken", params)["startPageToken"])

    def sync(self) -> list[RemoteChange]:
        """Bring the item tree up to date; returns what changed, by path.

        A long first listing saves progress every `SAVE_EVERY_PAGES` pages and resumes there.
        If Google no longer accepts the saved page token, the drive is listed again."""
        items = self._load()
        before = self._files(items)
        try:
            touched = self._run(items)
        except ResyncRequiredError:
            if self._cursor is None:
                raise
            log.info("Listing %s again from the start (Google's change token expired).",
                     self.drive_id)  # fmt: skip
            self._cursor = None
            touched = self._run(items)
        self._by_path = None
        return self._changes(before, items, touched)

    def _save(self, state: dict[str, str], upserts: list[CloudItem], removed: list[str],
              reset: bool) -> None:  # fmt: skip
        cursor = json.dumps(state, sort_keys=True)
        self.cache.save(self._root_id, cursor, upserts, removed, reset=reset)
        self._cursor = cursor

    def _run(self, items: dict[str, CloudItem]) -> set[str]:
        state: dict[str, str] = json.loads(self._cursor) if self._cursor else {}
        touched: set[str] = set()
        if "changes" not in state:
            self._list_all(items, state, touched)
            state = json.loads(self._cursor or "{}")
        self._apply_changes(items, state["changes"], touched)
        return touched

    def _list_all(self, items: dict[str, CloudItem], state: dict[str, str],
                  touched: set[str]) -> None:  # fmt: skip
        reset = not state
        if reset:
            items.clear()  # in memory only; the saved tree is replaced at the first save
            state = {"start": self._start_token(), "list": ""}
            root = CloudItem(self.drive_id, None, "", True, is_root=True, mime=FOLDER)
            items[root.id] = root
            upserts: list[CloudItem] = [root]
        else:
            upserts = []
        removed: list[str] = []
        unsaved = 0
        for page in self.client.pages("/files", self._list_params(), state["list"] or None):
            for data in page.get("files", []):
                item = _item(data)
                if item is None:
                    continue
                upserts.append(item)
                items[item.id] = item
                touched.add(item.id)
            unsaved += 1
            next_token = page.get("nextPageToken")
            if not next_token:
                self._save({"changes": state["start"]}, upserts, removed, reset)
                return
            if unsaved >= SAVE_EVERY_PAGES:
                self._save({"start": state["start"], "list": str(next_token)}, upserts,
                           removed, reset)  # fmt: skip
                reset, unsaved, upserts = False, 0, []

    def _apply_changes(self, items: dict[str, CloudItem], token: str, touched: set[str]) -> None:
        upserts: list[CloudItem] = []
        removed: list[str] = []
        unsaved = 0
        try:
            for page in self.client.pages("/changes", self._change_params(), token):
                for change in page.get("changes", []):
                    file_id = str(change.get("fileId") or "")
                    data = change.get("file")
                    item = None if change.get("removed") or not data else _item(data)
                    if item is None or file_id == self.drive_id:
                        if file_id and file_id != self.drive_id:
                            removed.append(file_id)
                            items.pop(file_id, None)
                        continue
                    upserts.append(item)
                    items[item.id] = item
                    touched.add(item.id)
                unsaved += 1
                done = page.get("newStartPageToken")
                if done:
                    self._save({"changes": str(done)}, upserts, removed, False)
                    return
                if unsaved >= SAVE_EVERY_PAGES and page.get("nextPageToken"):
                    self._save({"changes": str(page["nextPageToken"])}, upserts, removed, False)
                    unsaved, upserts, removed = 0, [], []
        except DriveError as exc:
            if exc.status in (400, 404, 410) and not isinstance(exc, ResyncRequiredError):
                raise ResyncRequiredError(str(exc), exc.status, exc.code) from None
            raise
        raise DriveError("Google didn't finish listing changes. Try again later.")

    # --- reading ---

    def _item(self, path: str) -> CloudItem:
        return self._load()[self._item_id(path)]

    def open_bytes(self, path: str) -> bytes:
        item = self._item(path)
        if item.mime in NATIVE:
            export = NATIVE[item.mime][1]
            if export is None:  # drawings, forms: nothing to search but the name
                return json.dumps({"doc_id": item.id}).encode()
            return self.client.get_bytes(f"/files/{quote(item.id)}/export", {"mimeType": export})
        return self.client.get_bytes(
            f"/files/{quote(item.id)}", {"alt": "media", "supportsAllDrives": "true"}
        )

    def versions(self, path: str) -> list[RemoteVersion]:
        """Earlier revisions Google keeps, newest first (the current one is left out)."""
        item = self._item(path)
        params = {
            "pageSize": "200", "supportsAllDrives": "true",
            "fields": "nextPageToken,"
            "revisions(id,modifiedTime,size,lastModifyingUser(displayName))",
        }  # fmt: skip
        revisions: list[dict[str, Any]] = []
        for page in self.client.pages(f"/files/{quote(item.id)}/revisions", params):
            revisions.extend(page.get("revisions", []))
        out = []
        for r in revisions[:-1]:  # Google lists oldest first; the last is the current one
            user = (r.get("lastModifyingUser") or {}).get("displayName")
            size = r.get("size")
            out.append(
                RemoteVersion(
                    id=str(r["id"]),
                    modified=iso_timestamp(r.get("modifiedTime")),
                    size=int(size) if size is not None else None,
                    author=str(user) if user else None,
                )
            )
        return sorted(out, key=lambda v: -v.modified)

    def open_version(self, path: str, version_id: str) -> bytes:
        item = self._item(path)
        url = f"/files/{quote(item.id)}/revisions/{quote(version_id, safe='')}"
        if item.mime in NATIVE:
            export = NATIVE[item.mime][1]
            links = self.client.get(url, {"fields": "exportLinks"}).get("exportLinks") or {}
            if export is None or export not in links:
                raise ConnectorError(f"Google can't export that version of {path} as text.")
            return self.client.get_bytes(str(links[export]))
        return self.client.get_bytes(url, {"alt": "media"})

    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]:
        """Changes since the last sync (every file when `cursor` is None). The cursor is
        the connector's own saved state, so this simply syncs."""
        if cursor is None:
            return [RemoteChange(f.path, "added", f) for f in self.list_files()], self._cursor or ""
        return self.sync(), self._cursor or ""

    @staticmethod
    def _remote(item: CloudItem, path: str) -> RemoteFile:
        return RemoteFile(path=path, size=item.size, modified=item.modified)
