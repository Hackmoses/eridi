"""The engine's public API. The CLI, the local web API and the MCP server call only this.

`Eridi` ties together the config, the record, the index, the snapshots and the storage
connectors. Every public method is safe to call from several threads (the web server
scans in the background while it answers requests).
"""

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from eridi.connectors import connector_for
from eridi.connectors.base import (
    Connector,
    ConnectorError,
    NameTakenInStorageError,
    SavingConnector,
)
from eridi.connectors.local import read_root_marker, write_root_marker
from eridi.connectors.msauth import MicrosoftAccount, MicrosoftAuth, WriteAccessNeededError
from eridi.connectors.msgraph import (
    DriveInfo,
    GraphClient,
    MsGraphConnector,
    list_drives,
    root_id_for,
)
from eridi.core import changes as changes_module
from eridi.core.changes import (
    BASELINE_PREFIX,
    Activity,
    FileDiff,
    PendingChange,
    file_stats,
    recent_activity,
)
from eridi.core.config import Config, User, config_path, load_config, save_config
from eridi.core.ids import is_ulid, new_id
from eridi.core.index.cloud import CURSOR_PREFIX, IndexDeltaCache
from eridi.core.index.db import check_fts5, get_meta, index_path, open_index, set_meta
from eridi.core.index.files import (
    file_moves,
    get_file,
    list_files,
    restore_versions,
    text_versions,
    upsert_root,
)
from eridi.core.index.projects import (
    entry_in_project_sql,
    file_in_project_sql,
    list_projects,
    projects_for_file,
)
from eridi.core.index.rebuild import RebuildReport, rebuild
from eridi.core.index.records import RecordSyncReport, sync_records
from eridi.core.index.search import SearchHit, search
from eridi.core.models import (
    OUTCOMES,
    Author,
    Entry,
    EntryChange,
    FileInfo,
    Project,
    ProjectFolder,
    ProjectRef,
    Root,
    Subject,
    utc_now,
)
from eridi.core.outbox import (
    ON_EXISTS,
    NameTakenError,
    Outbox,
    OutboxJob,
    SaveRefusedError,
    free_name,
)
from eridi.core.paths import PathOutsideRootError, app_home, normalize_relative, to_posix_relative
from eridi.core.record.schema import make_entry, parse_entry
from eridi.core.record.store import (
    MARKER_NAME,
    RecordFolderError,
    RecordStore,
    conflict_original,
)
from eridi.core.record.views import HistoryItem, history
from eridi.core.scan import Progress, ScanReport, scan_files, scan_root
from eridi.core.snapshots import SnapshotStore

log = logging.getLogger(__name__)

BUILT_KEY = "built_at"
RECORD_REFRESH_SECONDS = 2.0


class EridiError(Exception):
    """A problem the user can fix. The message says what went wrong and what to do."""


class NotInitializedError(EridiError):
    def __init__(self) -> None:
        super().__init__("Eridi isn't set up on this computer yet. Run `eridi init` first.")


class FileNotFoundInIndexError(EridiError):
    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(
            f"No file matches {ref!r}. Check the name, or run `eridi scan` if it's new."
        )


class AmbiguousFileError(EridiError):
    def __init__(self, ref: str, candidates: list[FileInfo]) -> None:
        self.ref = ref
        self.candidates = candidates
        names = ", ".join(file_id(f) for f in candidates[:5])
        more = f" and {len(candidates) - 5} more" if len(candidates) > 5 else ""
        super().__init__(
            f"{ref!r} matches {len(candidates)} files: {names}{more}. Use more of the path."
        )


class UnknownProjectError(EridiError):
    def __init__(self, ref: str) -> None:
        super().__init__(f"No project matches {ref!r}. See `eridi project list`.")


class SaveApprovalNeededError(EridiError):
    """Turning Save on for Microsoft storage needs the user to allow Eridi to write."""

    def __init__(self, root: Root) -> None:
        self.root_id = root.id
        super().__init__(
            f"To save to {root.label!r}, allow Eridi to write there: run "
            f"`eridi storage set {root.label!r} --save` in a terminal, or use Allow saving on "
            "the Storage page, and approve the Microsoft sign-in."
        )


class UnknownEntryError(EridiError):
    def __init__(self, entry_id: str) -> None:
        super().__init__(f"There is no record entry with ID {entry_id!r}.")


def file_id(file: FileInfo) -> str:
    """The stable ID used everywhere a file is named: `<root_id>:<path>`."""
    return f"{file.root_id}:{file.path}"


@dataclass
class ScanSummary:
    reports: list[ScanReport] = field(default_factory=list)
    record: RecordSyncReport | None = None
    evicted: int = 0
    rebuilt: bool = False

    def total(self, attribute: str) -> int:
        return sum(len(getattr(r, attribute)) for r in self.reports)


@dataclass(frozen=True)
class VersionInfo:
    text_sha256: str
    first_seen: str
    source: str
    available: bool
    """The text is still stored, so it can be diffed."""
    is_current: bool


@dataclass(frozen=True)
class FileHistory:
    file: FileInfo
    root_label: str | None
    versions: list[VersionInfo]
    entries: list[HistoryItem]
    moves: list[tuple[str, str, str]]
    """(from_path, to_path, moved_at)."""


@dataclass(frozen=True)
class ProjectOverview:
    project: Project
    files: list[FileInfo]
    """Present files in the project's folders, from every storage."""
    pending: list[PendingChange]
    entries: list[Entry]
    """The project's record timeline, newest first."""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    """"ok", "warning" or "error"."""
    detail: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "error" for c in self.checks)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(DoctorCheck(name, status, detail))


class Eridi:
    """Open with `Eridi()`; close with `close()` or use it as a context manager."""

    def __init__(self, config_file: Path | None = None, index_file: Path | None = None) -> None:
        self.config_file = config_file or config_path()
        self._config = load_config(self.config_file)
        self._config_stamp = self._stamp()
        self._lock = threading.RLock()
        self._index_path = index_file or index_path()
        self.conn: sqlite3.Connection = open_index(self._index_path, threads=True)
        self._scan_lock = threading.Lock()
        self.snapshots = SnapshotStore(cap_bytes=self._config.settings.snapshot_cap_mb * 2**20)
        self._store: RecordStore | None = None
        self._microsoft: MicrosoftAuth | None = None
        self.graph_client_factory: Callable[[Callable[[], str]], GraphClient] = GraphClient
        self._last_refresh = 0.0
        self.outbox = Outbox()
        self._outbox_lock = threading.Lock()
        self.last_activity: str | None = None
        """When a watcher last recorded a change (UIs refresh when this moves)."""

    def __enter__(self) -> "Eridi":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- setup ---

    @property
    def config(self) -> Config:
        self._reload_if_changed()
        return self._config

    def _stamp(self) -> int | None:
        try:
            return self.config_file.stat().st_mtime_ns
        except OSError:
            return None

    def _reload_if_changed(self) -> None:
        """Pick up changes another Eridi made (e.g. `eridi storage add` while the app runs)."""
        stamp = self._stamp()
        if stamp != self._config_stamp:
            self._config = load_config(self.config_file)
            self._config_stamp = stamp
            self.snapshots.cap_bytes = self._config.settings.snapshot_cap_mb * 2**20
            self._store = None

    def _save(self, config: Config) -> None:
        save_config(config, self.config_file)
        self._config_stamp = self._stamp()
        self._config = config
        self.snapshots.cap_bytes = config.settings.snapshot_cap_mb * 2**20

    @property
    def initialized(self) -> bool:
        return self.config_file.exists() and bool(self.config.user.name.strip())

    def init(
        self,
        name: str,
        email: str | None = None,
        record_folder: Path | None = None,
        *,
        join: bool = False,
    ) -> Config:
        """Set who is writing, and where the record lives (default `<app-data>/record`).

        `join=True` joins a record folder a labmate already created (for example in a shared
        SharePoint or Google Drive folder): it must exist and is never created.
        """
        if not name.strip():
            raise EridiError("A name is needed, so entries show who wrote them.")
        if join and record_folder is None:
            raise EridiError("Give the shared record folder to join.")
        with self._lock:
            folder = record_folder.expanduser().resolve() if record_folder else None
            config = replace(
                self._config, user=User(name.strip(), (email or "").strip() or None),
                record_folder=folder,
            )  # fmt: skip
            if join:
                assert folder is not None  # noqa: S101 - checked above
                self._store = self._open_shared(folder)
                self._save(config)
                self._resync_record()
                return config
            try:
                self._store = RecordStore.init(config.effective_record_folder)
            except OSError as exc:
                raise EridiError(
                    f"Could not create the record folder {config.effective_record_folder}: "
                    f"{exc.strerror or exc}. Choose a folder you can write to."
                ) from None
            self._save(config)
            self._resync_record()
            return config

    @staticmethod
    def _open_shared(folder: Path) -> RecordStore:
        if not folder.is_dir():
            raise EridiError(
                f"{folder} can't be found. If it's a shared folder, check that it has synced "
                "to this computer (in OneDrive, Google Drive or SharePoint, make it available "
                "offline), then try again."
            )
        try:
            return RecordStore(folder)
        except RecordFolderError:
            inside = sorted(p.parent for p in folder.glob(f"*/{MARKER_NAME}"))
            hint = (
                f" The record seems to be in {inside[0]}; join that folder instead."
                if inside
                else " Ask the labmate who set it up for the folder that contains "
                f"{MARKER_NAME}, or run `eridi init` without --join to start a new record."
            )
            raise EridiError(f"{folder} is not an Eridi record folder.{hint}") from None

    def record_members(self) -> list[tuple[str, int, str]]:
        """Who has written to the record: (name, entries, latest entry time), most recent
        first. AI-written entries count for the person they were written for."""
        with self._lock:
            self._refresh_records()
            rows = self.conn.execute(
                "SELECT json FROM entries WHERE kind != 'tombstone' ORDER BY id"
            ).fetchall()
        people: dict[str, tuple[int, str]] = {}
        for (raw,) in rows:
            entry = parse_entry(json.loads(raw))
            name = entry.author.name
            if entry.author.kind == "agent" and name.endswith(")") and " (for " in name:
                name = name.rsplit(" (for ", 1)[1][:-1]
            count, _ = people.get(name, (0, ""))
            people[name] = (count + 1, entry.created_at)
        return sorted(
            ((n, c, last) for n, (c, last) in people.items()), key=lambda t: t[2], reverse=True
        )

    def _resync_record(self) -> None:
        """The record folder changed: load it fully (dropping another folder's entries)."""
        assert self._store is not None  # noqa: S101 - set just before
        sync_records(self.conn, self._store, full=True)
        self._last_refresh = time.monotonic()

    def _require_init(self) -> None:
        if not self.initialized:
            raise NotInitializedError()

    @property
    def store(self) -> RecordStore:
        if self._store is None:
            folder = self._config.effective_record_folder
            if self._config.record_folder is None:
                self._store = RecordStore.init(folder)
            else:
                try:
                    self._store = RecordStore(folder)
                except RecordFolderError as exc:
                    raise EridiError(
                        f"{exc} If it's in a synced folder, check that the sync app is running."
                    ) from None
        return self._store

    # --- storage ---

    def list_roots(self) -> list[Root]:
        self._reload_if_changed()
        return list(self._config.roots)

    def get_root(self, root_id: str) -> Root:
        root = self._config.root(root_id)
        if root is None:
            raise EridiError(f"There is no storage with ID {root_id!r}. See `eridi storage list`.")
        return root

    def add_root(
        self,
        path: Path,
        label: str | None = None,
        *,
        mark_folder: bool = False,
        search: bool = True,
        save: bool = False,
    ) -> Root:
        """Connect a local or synced folder.

        `mark_folder=True` writes `.eridi-root.json` into it, so other computers recognize the
        same synced folder. Without it, nothing is written there.
        """
        folder = path.expanduser().resolve()
        if not folder.is_dir():
            raise EridiError(f"{path} is not a folder. Give the path of a folder to search.")
        with self._lock:
            for other in self._config.roots:
                if other.kind != "local":
                    continue
                existing = Path(other.location)
                if folder == existing:
                    raise EridiError(f"{folder} is already connected as {other.label!r}.")
                if folder.is_relative_to(existing) or existing.is_relative_to(folder):
                    raise EridiError(
                        f"{folder} overlaps {other.label!r} ({existing}). "
                        "Connect folders that don't contain each other."
                    )
            marker = read_root_marker(folder)
            if marker is not None:
                root_id, marker_label = marker
                label = label or marker_label
                if self._config.root(root_id) is not None:
                    raise EridiError(f"{folder} is already connected (storage {root_id}).")
            else:
                root_id = new_id("root")
                label = label or folder.name
                if mark_folder:
                    try:
                        write_root_marker(folder, root_id, label)
                    except OSError as exc:
                        raise EridiError(
                            f"Could not write the folder marker in {folder}: "
                            f"{exc.strerror or exc}. Add it without marking the folder."
                        ) from None
            root = Root(root_id, label, str(folder), search=search, save=save)
            self._save(self._config.with_root(root))
            with self.conn:
                upsert_root(self.conn, root)
            return root

    def update_root(
        self, root_id: str, *, label: str | None = None, search: bool | None = None,
        save: bool | None = None,
    ) -> Root:  # fmt: skip
        with self._lock:
            root = self.get_root(root_id)
            if save and not root.save and root.kind == "msgraph":
                try:
                    self.microsoft.token(root.account, write=True)
                except WriteAccessNeededError:
                    raise SaveApprovalNeededError(root) from None
            root = replace(
                root,
                label=label if label is not None else root.label,
                search=search if search is not None else root.search,
                save=save if save is not None else root.save,
            )
            self._save(self._config.with_root(root))
            with self.conn:
                upsert_root(self.conn, root)
            return root

    def root_marker(self, root_id: str) -> str | None:
        """The storage ID in a local folder's `.eridi-root.json`, if it has one."""
        root = self.get_root(root_id)
        if root.kind != "local":
            return None
        marker = read_root_marker(Path(root.location))
        return marker[0] if marker else None

    def mark_root(self, root_id: str) -> Root:
        """Let other computers recognize this storage folder, even at a different path.

        Writes `.eridi-root.json` (the only file Eridi ever writes into storage, and only
        when asked). If the folder already has a marker from another computer with a
        different ID, this computer switches to that ID instead, so everyone's notes about
        its files line up; its files are re-read on the next scan.
        """
        with self._lock:
            root = self.get_root(root_id)
            if root.kind != "local":
                raise EridiError(f"{root.label!r} is cloud storage; it is recognized by its ID.")
            folder = Path(root.location)
            marker = read_root_marker(folder)
            if marker is None:
                try:
                    write_root_marker(folder, root.id, root.label)
                except OSError as exc:
                    raise EridiError(
                        f"Could not write the folder marker in {folder}: "
                        f"{exc.strerror or exc}. Check that you can write to that folder."
                    ) from None
                return root
            shared_id = marker[0]
            if shared_id == root.id:
                return root
            if self._config.root(shared_id) is not None:
                raise EridiError(
                    f"{folder} is marked as storage {shared_id}, which is already connected "
                    "here at another path. Remove one of them with `eridi storage remove`."
                )
            self.remove_root(root.id)
            switched = replace(root, id=shared_id)
            self._save(self._config.with_root(switched))
            with self.conn:
                upsert_root(self.conn, switched)
            return switched

    def remove_root(self, root_id: str) -> None:
        """Disconnect storage. Its files leave the index; the record keeps everything said
        about them."""
        with self._lock:
            self.get_root(root_id)
            roots = tuple(r for r in self._config.roots if r.id != root_id)
            self._save(replace(self._config, roots=roots))
            with self.conn:
                self.conn.execute(
                    "DELETE FROM files_fts WHERE rowid IN (SELECT id FROM files WHERE root_id = ?)",
                    (root_id,),
                )
                self.conn.execute("DELETE FROM files WHERE root_id = ?", (root_id,))
                self.conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
                self.conn.execute("DELETE FROM cloud_items WHERE root_id = ?", (root_id,))
                self.conn.execute("DELETE FROM meta WHERE key = ?", (CURSOR_PREFIX + root_id,))

    def _own_folders_ignored(self, root: Root) -> list[str]:
        """Patterns that keep Eridi's own folders out of a root that happens to contain them."""
        patterns = []
        location = Path(root.location)
        for own in (self._config.effective_record_folder, app_home()):
            try:
                patterns.append("/" + to_posix_relative(location, own) + "/")
            except (PathOutsideRootError, OSError):
                continue
        return patterns

    def _split_file_id(self, ref: str) -> tuple[str, str] | None:
        """`<root_id>:<path>` → (root_id, path) for a connected storage. Storage IDs can
        contain ":" themselves (`ms:<driveId>`), so match the longest known one."""
        for root in sorted(self._config.roots, key=lambda r: -len(r.id)):
            if ref.startswith(root.id + ":"):
                return root.id, ref[len(root.id) + 1 :]
        return None

    def connector(self, root: Root) -> Connector:
        if root.kind == "msgraph":
            return self._graph_connector(root)
        return connector_for(root, self._config.settings, ignore=self._own_folders_ignored(root))

    # --- Microsoft OneDrive and SharePoint ---

    @property
    def microsoft(self) -> MicrosoftAuth:
        """Microsoft sign-in (created on first use; tests may replace it)."""
        if self._microsoft is None:
            self._microsoft = MicrosoftAuth(self._config.settings.microsoft_client_id)
        return self._microsoft

    @microsoft.setter
    def microsoft(self, auth: MicrosoftAuth) -> None:
        self._microsoft = auth

    def _graph(self, account_id: str) -> GraphClient:
        auth = self.microsoft
        return self.graph_client_factory(lambda: auth.token(account_id))

    def _delta_cache(self) -> IndexDeltaCache:
        if str(self._index_path) == ":memory:":
            return IndexDeltaCache(conn=self.conn)
        return IndexDeltaCache(self._index_path)

    def _graph_connector(self, root: Root) -> MsGraphConnector:
        if not root.account:
            raise ConnectorError(
                f"{root.label!r} has no Microsoft account. Remove it and add it again with "
                "`eridi storage add-microsoft`."
            )
        writer = None
        if root.save:
            auth, account = self.microsoft, root.account
            writer = self.graph_client_factory(lambda: auth.token(account, write=True))
        return MsGraphConnector(
            root.id, root.location, self._graph(root.account), cache=self._delta_cache(),
            writer=writer,
        )  # fmt: skip

    def allow_microsoft_saving(
        self, root_id: str, *, device_code: bool = False, show: Callable[[str], None] = print
    ) -> Root:
        """Ask the user to let Eridi write to a Microsoft storage, then turn Save on."""
        root = self.get_root(root_id)
        if root.kind != "msgraph":
            return self.update_root(root_id, save=True)
        self.microsoft.allow_saving(root.account, device_code=device_code, show=show)
        return self.update_root(root_id, save=True)

    def _microsoft_status(self, root: Root) -> tuple[str, str]:
        """Doctor's check for a Microsoft drive, without going online."""
        try:
            accounts = {a.id: a.username for a in self.microsoft.accounts()}
        except ConnectorError as exc:
            return "error", str(exc)
        if root.account not in accounts:
            return "warning", (
                "Not signed in to Microsoft on this computer. Sign in again with "
                "`eridi storage add-microsoft`."
            )
        state = "search on" if root.search else "search off"
        return "ok", f"Microsoft, as {accounts[root.account]} ({state})."

    def microsoft_sign_in(
        self, *, device_code: bool = False, show: Callable[[str], None] = print
    ) -> tuple[MicrosoftAccount, list[DriveInfo]]:
        """Sign in to Microsoft; return the account and the drives it can read."""
        account = self.microsoft.sign_in(device_code=device_code, show=show)
        return account, self.microsoft_drives(account.id)

    def microsoft_drives(self, account_id: str) -> list[DriveInfo]:
        """The account's OneDrive and the libraries of SharePoint sites they follow."""
        return list_drives(self._graph(account_id))

    def add_microsoft_root(
        self,
        account_id: str,
        drive: DriveInfo | str,
        label: str | None = None,
        *,
        search: bool = True,
    ) -> Root:
        """Connect a OneDrive or SharePoint library. Nothing is downloaded until a scan."""
        if isinstance(drive, str):
            found = next((d for d in self.microsoft_drives(account_id) if d.id == drive), None)
            if found is None:
                raise EridiError(
                    f"No OneDrive or followed SharePoint library has ID {drive!r}. "
                    "To add a SharePoint library, follow its site in SharePoint first."
                )
            drive = found
        with self._lock:
            root_id = root_id_for(drive.id)
            existing = self._config.root(root_id)
            if existing is not None:
                raise EridiError(f"{drive.label} is already connected as {existing.label!r}.")
            root = Root(
                root_id, (label or drive.label).strip() or drive.label, drive.id,
                kind="msgraph", search=search, account=account_id,
            )  # fmt: skip
            self._save(self._config.with_root(root))
            with self.conn:
                upsert_root(self.conn, root)
            return root

    # --- saving through the outbox (M11) ---

    def save_target(
        self, *, project: str | None = None, storage: str | None = None, folder: str | None = None
    ) -> tuple[Root, str, Project | None]:
        """Where a file saved for this project (or into this storage) goes: the storage and
        the folder inside it. A project saves to its home storage, into the project's
        folder there if it has one."""
        proj = self._get_project(project) if project is not None else None
        if storage is None:
            if proj is None:
                raise EridiError("Say where to save: a project, or a storage.")
            if proj.home_root_id is None:
                raise EridiError(
                    f"{proj.name!r} has no home storage to save to. Choose one with "
                    f"`eridi project home {proj.name!r} <storage>`."
                )
            storage = proj.home_root_id
        root = self.get_root(storage)
        if folder is None:
            own = [f.path for f in proj.folders if f.root_id == root.id] if proj else []
            folder = min(own, key=len) if own else ""
        folder = folder.strip().strip("/\\")
        if folder:
            try:
                folder = normalize_relative(folder)
            except ValueError as exc:
                raise EridiError(f"Can't save into that folder: {exc}") from None
        self._check_can_save(root, proj)
        return root, folder, proj

    def _check_can_save(self, root: Root, project: Project | None) -> None:
        if not root.save:
            raise SaveRefusedError(
                f"Saving to {root.label!r} is off. Turn it on with "
                f"`eridi storage set {root.label!r} --save` (or on the Storage page)."
            )

    def save_file(
        self,
        data: bytes,
        name: str,
        *,
        project: str | None = None,
        storage: str | None = None,
        folder: str | None = None,
        on_exists: str = "ask",
        upload: bool = True,
    ) -> OutboxJob:
        """Save a file into a project's home storage (or a given storage) through the outbox.

        The bytes are kept on this computer first, then uploaded; if that fails they stay
        queued and are retried. Nothing is ever replaced unless `on_exists` says so."""
        self._require_init()
        if on_exists not in ON_EXISTS:
            raise EridiError(f"on_exists must be one of: {', '.join(ON_EXISTS)}.")
        clean = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not clean or clean in (".", ".."):
            raise EridiError("The file needs a name.")
        with self._lock:
            self._refresh_records()
            try:
                root, folder, proj = self.save_target(
                    project=project, storage=storage, folder=folder
                )
            except SaveRefusedError as exc:
                raise EridiError(str(exc)) from None
            path = normalize_relative(f"{folder}/{clean}" if folder else clean)
            job = self.outbox.add(
                root.id, path, data, on_exists=on_exists, project_id=proj.id if proj else None
            )
        if upload:
            job = self._run_job(job)
        return job

    def outbox_jobs(self) -> list[OutboxJob]:
        """Files saved through Eridi, oldest first: waiting, needing a decision, or done."""
        return self.outbox.jobs()

    def get_save(self, job_id: str) -> OutboxJob:
        job = self.outbox.get(job_id.strip().upper())
        if job is None:
            raise EridiError(f"No saved file has ID {job_id!r}. See `eridi outbox`.")
        return job

    def retry_save(
        self, job_id: str, on_exists: str | None = None, *, upload: bool = True
    ) -> OutboxJob:
        """Try again now; `on_exists` answers "the name is taken" (replace or rename)."""
        job = self.get_save(job_id)
        if job.done:
            return job
        if on_exists is not None and on_exists not in ON_EXISTS:
            raise EridiError(f"on_exists must be one of: {', '.join(ON_EXISTS)}.")
        retried = self.outbox.retry(job.id, on_exists)
        if retried is None:  # cancelled meanwhile
            raise EridiError(f"No saved file has ID {job_id!r}. See `eridi outbox`.")
        return self._run_job(retried) if upload else retried

    def cancel_save(self, job_id: str) -> OutboxJob:
        """Stop uploading a file and delete Eridi's local copy of it (never the storage's)."""
        job = self.get_save(job_id)
        if job.done:
            raise EridiError("That file is already uploaded; there is nothing to cancel.")
        self.outbox.cancel(job.id)
        return job

    def process_outbox(self) -> list[OutboxJob]:
        """Upload every saved file that is due (the app calls this in the background)."""
        if not self._outbox_lock.acquire(blocking=False):
            return []
        try:
            done = self.outbox.process(self._upload)
        finally:
            self._outbox_lock.release()
        self._after_upload(done)
        return done

    def _run_job(self, job: OutboxJob) -> OutboxJob:
        with self._outbox_lock:
            result = self.outbox.run(job, self._upload)
        self._after_upload([result])
        return result

    def _after_upload(self, jobs: list[OutboxJob]) -> None:
        """Index uploaded files at once, so they're searchable and in their project."""
        by_root: dict[str, list[str]] = {}
        for job in jobs:
            if job.done and job.uploaded_path:
                by_root.setdefault(job.root_id, []).append(job.uploaded_path)
        for root_id, paths in by_root.items():
            try:
                self.scan_changed(root_id, paths)
            except (ConnectorError, EridiError) as exc:
                log.info("Saved files will be indexed at the next scan: %s", exc)

    def _upload(self, job: OutboxJob, data: bytes) -> str:
        with self._lock:
            self._reload_if_changed()
            root = self._config.root(job.root_id)
            if root is None:
                raise SaveRefusedError(
                    "That storage is no longer connected. Save the file somewhere else."
                )
            project = None
            if job.project_id is not None:
                self._refresh_records()
                project = next(
                    (p for p in list_projects(self.conn) if p.id == job.project_id), None
                )
            self._check_can_save(root, project)
            connector = self.connector(root)
        if not isinstance(connector, SavingConnector):
            raise SaveRefusedError(f"Eridi can't save to {root.label!r} ({root.kind}) yet.")
        try:
            return self._put(connector, job, data)
        except WriteAccessNeededError as exc:
            raise SaveRefusedError(str(exc)) from None

    def _put(self, connector: SavingConnector, job: OutboxJob, data: bytes) -> str:
        path = job.path
        if job.on_exists == "rename" and connector.exists(path):
            if _same_bytes(connector, path, job):
                return path  # a crash after the upload, before it was recorded
            path = free_name(path, connector.exists)
        try:
            connector.save(path, data, overwrite=job.on_exists == "replace")
        except NameTakenInStorageError:
            if _same_bytes(connector, path, job):
                return path
            raise NameTakenError(path) from None
        return path

    # --- projects ---

    def list_projects(self) -> list[Project]:
        with self._lock:
            self._refresh_records()
            return list_projects(self.conn)

    def get_project(self, ref: str) -> Project:
        """Find a project by ID or name (exact, then the start or any part of the name)."""
        with self._lock:
            self._refresh_records()
            return self._get_project(ref)

    def _get_project(self, ref: str) -> Project:
        ref = ref.strip()
        projects = list_projects(self.conn)
        for p in projects:
            if p.id == ref:
                return p
        needle = ref.casefold()
        for match in (
            lambda p: p.name.casefold() == needle,
            lambda p: p.name.casefold().startswith(needle),
            lambda p: needle in p.name.casefold(),
        ):
            hits = [p for p in projects if match(p)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                names = ", ".join(repr(p.name) for p in hits[:5])
                raise EridiError(f"{ref!r} matches several projects: {names}. Use the full name.")
        raise UnknownProjectError(ref)

    def _project_id(self, ref: str | None) -> str | None:
        return None if ref is None else self._get_project(ref).id

    def _write_project(self, project: Project, text: str = "") -> Project:
        ref = ProjectRef(project.id, project.name, project.home_root_id, project.folders)
        entry = make_entry(
            "project", self._author("human", None), self._config.device_id, text,
            project=ref, project_id=project.id,
        )  # fmt: skip
        self._append(entry)
        return self._get_project(project.id)

    def _check_name(self, name: str, except_id: str | None = None) -> str:
        name = " ".join(name.split())
        if not name:
            raise EridiError("A project needs a name.")
        for p in list_projects(self.conn):
            if p.id != except_id and p.name.casefold() == name.casefold():
                raise EridiError(f"There is already a project called {p.name!r}.")
        return name

    def create_project(
        self, name: str, *, home_root_id: str | None = None, folders: Iterable[str] = ()
    ) -> Project:
        """Start a project. `folders` are folder references, as for `add_project_folder`."""
        self._require_init()
        with self._lock:
            self._refresh_records(force=True)
            name = self._check_name(name)
            if home_root_id is not None:
                self.get_root(home_root_id)
            resolved = tuple(dict.fromkeys(self._resolve_folder(f) for f in folders))
            project = Project(new_id("proj"), name, home_root_id, resolved)
            return self._write_project(project)

    def rename_project(self, ref: str, name: str) -> Project:
        self._require_init()
        with self._lock:
            self._refresh_records(force=True)
            project = self._get_project(ref)
            name = self._check_name(name, except_id=project.id)
            return self._write_project(replace(project, name=name))

    def set_project_home(self, ref: str, root_id: str | None) -> Project:
        """Set the storage that new files for this project are saved to (used from M11)."""
        self._require_init()
        with self._lock:
            self._refresh_records(force=True)
            project = self._get_project(ref)
            if root_id is not None:
                self.get_root(root_id)
            return self._write_project(replace(project, home_root_id=root_id))

    def _resolve_folder(self, ref: str) -> ProjectFolder:
        """A folder from a path on disk, `<root_id>:<path>`, or a storage ID or name (all
        of it)."""
        ref = ref.strip()
        if not ref:
            raise EridiError("Give a folder: a path, or a storage name for all of it.")
        split = self._split_file_id(ref)
        if split is not None:
            root_id, rest = split
            path = rest.replace("\\", "/").strip("/")
            return ProjectFolder(root_id, normalize_relative(path) if path else "")
        for root in self._config.roots:
            if ref in (root.id, root.label):
                return ProjectFolder(root.id, "")
        candidate = Path(ref).expanduser()
        for root in self._config.roots:
            if root.kind != "local":
                continue
            location = Path(root.location)
            try:
                resolved = candidate.resolve()
                if resolved == location.resolve():
                    return ProjectFolder(root.id, "")
                rel = to_posix_relative(location, resolved)
            except (PathOutsideRootError, OSError, ValueError):
                continue
            if not resolved.is_dir():
                raise EridiError(f"{ref} is a file. Give the folder that holds it.")
            return ProjectFolder(root.id, rel)
        raise EridiError(
            f"{ref!r} isn't inside any connected storage. Connect it first with "
            "`eridi storage add`, or give `<storage ID>:<folder>`."
        )

    def add_project_folder(self, ref: str, folder: str) -> Project:
        """Add a folder to a project. Its files, and notes about them, join the project."""
        self._require_init()
        with self._lock:
            self._refresh_records(force=True)
            project = self._get_project(ref)
            new = self._resolve_folder(folder)
            if new in project.folders:
                return project
            return self._write_project(replace(project, folders=(*project.folders, new)))

    def remove_project_folder(self, ref: str, folder: str) -> Project:
        self._require_init()
        with self._lock:
            self._refresh_records(force=True)
            project = self._get_project(ref)
            old = self._resolve_folder(folder)
            if old not in project.folders:
                raise EridiError(f"That folder isn't part of {project.name!r}.")
            kept = tuple(f for f in project.folders if f != old)
            return self._write_project(replace(project, folders=kept))

    def projects_for(self, file: FileInfo) -> list[Project]:
        """Projects a file belongs to through its folder, most specific first."""
        with self._lock:
            return projects_for_file(list_projects(self.conn), file.root_id, file.path)

    def project_files(self, ref: str) -> list[FileInfo]:
        with self._lock:
            project = self._get_project(ref)
            rows = self.conn.execute(
                "SELECT root_id, path FROM files f WHERE f.status = 'present' AND "  # noqa: S608
                + file_in_project_sql("f")
                + " ORDER BY f.modified DESC, f.path",
                (project.id,),
            ).fetchall()
            found = (get_file(self.conn, r[0], r[1]) for r in rows)
            return [f for f in found if f is not None]

    def project_overview(self, ref: str, limit: int = 50) -> ProjectOverview:
        """Everything on a project page: files from all storage, changes waiting for a why,
        and the record timeline."""
        with self._lock:
            self._refresh_records()
            project = self._get_project(ref)
            return ProjectOverview(
                project,
                self.project_files(project.id),
                self.pending_changes(project.id),
                self.recent(limit, project=project.id),
            )

    # --- scanning and the index ---

    def _refresh_records(self, *, force: bool = False) -> RecordSyncReport | None:
        """Pick up entries other devices wrote. Cheap, and throttled."""
        now = time.monotonic()
        if not force and now - self._last_refresh < RECORD_REFRESH_SECONDS:
            return None
        self._last_refresh = now
        try:
            return sync_records(self.conn, self.store)
        except EridiError as exc:
            log.warning("Record folder not available: %s", exc)
            return None

    def refresh_record(self) -> list[Entry]:
        """Read entries that arrived in the record folder (from labmates or other computers)
        now, and return them oldest first."""
        with self._lock:
            report = self._refresh_records(force=True)
            if report is None or not report.added_ids:
                return []
            self.last_activity = utc_now()
            found = (self._get_entry(i) for i in report.added_ids)
            return [e for e in found if e is not None]

    def _referenced_texts(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT text_sha256 FROM files WHERE text_sha256 IS NOT NULL "
            "UNION SELECT change_to_text_sha256 FROM entries "
            "WHERE change_to_text_sha256 IS NOT NULL"
        ).fetchall()
        referenced = {r[0] for r in rows}
        for (raw,) in self.conn.execute("SELECT json FROM entries WHERE json LIKE '%from_text%'"):
            change = parse_entry(json.loads(raw)).change
            if change is not None and change.from_text_sha256:
                referenced.add(change.from_text_sha256)
        return referenced

    def scan(self, root_id: str | None = None, progress: Progress | None = None) -> ScanSummary:
        """Scan storage (all roots with search on, or one), then refresh the record.

        The scan writes through its own database connection without holding the service
        lock, so searches and notes keep working (on what's indexed so far) while a long
        scan runs. Only one scan runs at a time."""
        with self._scan_lock:
            with self._lock:
                self._reload_if_changed()
                first = get_meta(self.conn, BUILT_KEY) is None
                everything = first or root_id is None
                roots = self.list_roots() if everything else [self.get_root(str(root_id))]
                settings = self._config.settings
                summary = ScanSummary(rebuilt=first)
                connectors: list[tuple[Root, Connector]] = []
                for root in roots:
                    if not root.search:
                        with self.conn:
                            upsert_root(self.conn, root)
                        continue
                    try:
                        connectors.append((root, self.connector(root)))
                    except ConnectorError as exc:
                        summary.reports.append(ScanReport(root.id, unavailable=str(exc)))

            conn = self._scan_connection()
            try:
                for root, connector in connectors:
                    report = scan_root(conn, root, connector, self.snapshots, settings, progress)
                    summary.reports.append(report)
                    if report.new or report.changed or report.moved or report.missing:
                        self.last_activity = utc_now()
                    if report.unavailable is None:
                        _mark_baseline(conn, root.id)
            finally:
                if conn is not self.conn:
                    conn.close()

            with self._lock:
                if first:
                    # A new (or upgraded) index: restore version history and the whole record.
                    with self.conn:
                        restore_versions(self.conn, self.snapshots.read_log())
                    try:
                        summary.record = sync_records(self.conn, self.store, full=True)
                    except EridiError as exc:
                        log.warning("Record folder not available: %s", exc)
                    with self.conn:
                        set_meta(self.conn, BUILT_KEY, str(time.time()))
                    self._last_refresh = time.monotonic()
                else:
                    summary.record = self._refresh_records(force=True)
                summary.evicted = len(self.snapshots.evict(self._referenced_texts()))
                return summary

    def scan_changed(self, root_id: str, paths: Iterable[str]) -> ScanReport | None:
        """Scan just these files of one root (a watcher saw them change). Skipped while a
        full scan runs, which will see them anyway."""
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            with self._lock:
                self._reload_if_changed()
                root = self._config.root(root_id)
                if root is None or not root.search or get_meta(self.conn, BUILT_KEY) is None:
                    return None
                connector = self.connector(root)
                settings = self._config.settings
            conn = self._scan_connection()
            try:
                report = scan_files(conn, root, connector, self.snapshots, paths, settings)
            finally:
                if conn is not self.conn:
                    conn.close()
            with self._lock:
                if report.new or report.changed or report.moved or report.missing:
                    self._refresh_records(force=True)
                    self.last_activity = utc_now()
            return report
        finally:
            self._scan_lock.release()

    def activity(self, limit: int = 50, since: str | None = None) -> list[Activity]:
        """What changed recently across all storage, newest first."""
        with self._lock:
            self._refresh_records()
            return recent_activity(
                self.conn, self.snapshots, self._config.dismissed_changes, limit, since
            )

    def _scan_connection(self) -> sqlite3.Connection:
        if str(self._index_path) == ":memory:":
            return self.conn
        return open_index(self._index_path)

    def rebuild_index(self, progress: Progress | None = None) -> RebuildReport:
        """Throw the index away and rebuild it from storage, snapshots and the record."""
        with self._scan_lock, self._lock:
            self._reload_if_changed()
            roots = self.list_roots()
            connectors: dict[str, Connector] = {}
            for root in roots:
                with contextlib.suppress(ConnectorError):
                    connectors[root.id] = self.connector(root)
            try:
                store: RecordStore | None = self.store
            except EridiError:
                store = None
            report = rebuild(
                self.conn, roots, store, self.snapshots, self._config.settings, connectors,
                progress,
            )  # fmt: skip
            with self.conn:
                set_meta(self.conn, BUILT_KEY, str(time.time()))
            for root_id, scanned in report.roots.items():
                if scanned.unavailable is None:
                    _mark_baseline(self.conn, root_id)
            self._last_refresh = time.monotonic()
            return report

    # --- finding things ---

    def search(self, query: str, limit: int = 20, project: str | None = None) -> list[SearchHit]:
        """Search files and notes. File hits carry their history (`SearchHit.stats`)."""
        with self._lock:
            self._refresh_records()
            hits = search(self.conn, query, limit, self._project_id(project))
            dismissed = self._config.dismissed_changes
            return [
                replace(h, stats=file_stats(self.conn, h.root_id, h.path, dismissed))
                if h.kind == "file" and h.root_id and h.path
                else h
                for h in hits
            ]

    def resolve_file(self, ref: str) -> FileInfo:
        """Find a file by ID (`root_id:path`), path, path on disk, or part of its name."""
        with self._lock:
            return self._resolve_file(ref)

    def _resolve_file(self, ref: str) -> FileInfo:
        ref = ref.strip()
        if not ref:
            raise FileNotFoundInIndexError(ref)
        split = self._split_file_id(ref)
        if split is not None:
            found = get_file(self.conn, *split)
            if found is not None:
                return found
        # A path on disk inside a connected folder.
        candidate = Path(ref).expanduser()
        for root in self._config.roots:
            if root.kind != "local":
                continue
            try:
                rel = to_posix_relative(Path(root.location), candidate.resolve())
            except (PathOutsideRootError, OSError, ValueError):
                continue
            found = get_file(self.conn, root.id, rel)
            if found is not None:
                return found
        everything = list_files(self.conn)
        needle = ref.replace("\\", "/").strip("/")
        for match in (
            lambda f: f.path == needle,
            lambda f: f.path.lower() == needle.lower(),
            lambda f: f.path.rsplit("/", 1)[-1].lower() == needle.lower(),
            lambda f: f.path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() == needle.lower(),
            lambda f: needle.lower() in f.path.lower(),
        ):
            hits = [f for f in everything if match(f)]
            present = [f for f in hits if f.status == "present"]
            hits = present or hits
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise AmbiguousFileError(ref, hits)
        raise FileNotFoundInIndexError(ref)

    def find_files(self, ref: str, limit: int = 20) -> list[FileInfo]:
        """Files whose path contains `ref` (for pick lists)."""
        needle = ref.replace("\\", "/").lower()
        with self._lock:
            return [f for f in list_files(self.conn) if needle in f.path.lower()][:limit]

    def get_file(self, ref: str) -> FileInfo:
        return self.resolve_file(ref)

    def root_label(self, root_id: str) -> str | None:
        root = self._config.root(root_id)
        return root.label if root else None

    # --- changes ---

    def pending_changes(self, project: str | None = None) -> list[PendingChange]:
        with self._lock:
            self._refresh_records()
            return changes_module.pending_changes(
                self.conn, self.snapshots, self._config.dismissed_changes, self._project_id(project)
            )

    def pending_change_for(self, file: FileInfo) -> PendingChange | None:
        return next((c for c in self.pending_changes() if c.file_id == file.id), None)

    def diff(self, ref: str, from_sha: str | None = None, to_sha: str | None = None) -> FileDiff:
        with self._lock:
            file = self._resolve_file(ref)
            return changes_module.file_diff(self.conn, self.snapshots, file, from_sha, to_sha)

    def dismiss(self, change: PendingChange | str) -> None:
        """Stop asking about a change. Stored on this computer only."""
        with self._lock:
            key = change if isinstance(change, str) else change.key
            if key not in self._config.dismissed_changes:
                self._save(
                    replace(self._config, dismissed_changes=(*self._config.dismissed_changes, key))
                )

    def dismiss_all(self) -> int:
        """Dismiss every pending change (e.g. after first connecting old folders)."""
        with self._lock:
            keys = [c.key for c in self.pending_changes()]
            new = [k for k in keys if k not in self._config.dismissed_changes]
            if new:
                self._save(
                    replace(self._config, dismissed_changes=(*self._config.dismissed_changes, *new))
                )
            return len(keys)

    # --- writing to the record ---

    def _author(self, kind: str, agent_name: str | None) -> Author:
        user = self._config.user
        if kind == "agent":
            name = f"{agent_name or 'AI assistant'} (for {user.name})"
            return Author(name, "agent", user.email)
        return Author(user.name, "human", user.email)

    def _subject(self, file: FileInfo) -> Subject:
        return Subject(
            file.root_id,
            file.path,
            root_label=self.root_label(file.root_id),
            content_sha256=file.content_sha256,
            text_sha256=file.text_sha256,
        )

    def add_note(
        self,
        text: str,
        *,
        file: str | None = None,
        outcome: str | None = None,
        tags: Iterable[str] = (),
        project: str | None = None,
        kind: str = "note",
        supersedes: str | None = None,
        author_kind: str = "human",
        agent_name: str | None = None,
    ) -> Entry:
        """Write a note (or decision). With a file, it explains that file's pending change."""
        self._require_init()
        if not text.strip():
            raise EridiError("The note is empty. Say what changed and why.")
        if outcome is not None and outcome not in OUTCOMES:
            raise EridiError(f"Outcome must be one of: {', '.join(OUTCOMES)}.")
        with self._lock:
            subjects: list[Subject] = []
            change: EntryChange | None = None
            project_id = self._project_id(project)
            if file is not None:
                info = self._resolve_file(file)
                subjects.append(self._subject(info))
                if project_id is None:  # a note about a file joins the file's project
                    found = projects_for_file(list_projects(self.conn), info.root_id, info.path)
                    project_id = found[0].id if found else None
                pending = next(
                    (
                        c
                        for c in changes_module.pending_changes(self.conn, self.snapshots)
                        if c.file_id == info.id
                    ),
                    None,
                )
                if pending is not None:
                    change = EntryChange(
                        pending.from_text_sha256, pending.to_text_sha256, pending.summary
                    )
            if supersedes is not None and self._get_entry(supersedes) is None:
                raise UnknownEntryError(supersedes)
            entry = make_entry(
                kind,
                self._author(author_kind, agent_name),
                self._config.device_id,
                text.strip(),
                subjects=subjects,
                change=change,
                outcome=outcome,
                tags=list(tags),
                project_id=project_id,
                supersedes=supersedes,
            )
            return self._append(entry)

    def add_outcome(
        self,
        outcome: str,
        *,
        entry_id: str | None = None,
        file: str | None = None,
        text: str = "",
        author_kind: str = "human",
        agent_name: str | None = None,
    ) -> Entry:
        """Record whether something worked, for an earlier entry or a file's current version."""
        self._require_init()
        if outcome not in OUTCOMES:
            raise EridiError(f"Outcome must be one of: {', '.join(OUTCOMES)}.")
        if (entry_id is None) == (file is None):
            raise EridiError("Give either the entry or the file the outcome is about.")
        with self._lock:
            subjects: list[Subject] = []
            if entry_id is not None and self._get_entry(entry_id) is None:
                raise UnknownEntryError(entry_id)
            if file is not None:
                subjects.append(self._subject(self._resolve_file(file)))
            entry = make_entry(
                "outcome",
                self._author(author_kind, agent_name),
                self._config.device_id,
                text.strip(),
                outcome=outcome,
                target_id=entry_id,
                subjects=subjects,
            )
            return self._append(entry)

    def retract(self, entry_id: str, reason: str = "") -> Entry:
        """Retract an entry (it stays in history, labeled retracted)."""
        self._require_init()
        with self._lock:
            if self._get_entry(entry_id) is None:
                raise UnknownEntryError(entry_id)
            entry = make_entry(
                "tombstone", self._author("human", None), self._config.device_id, reason,
                target_id=entry_id,
            )  # fmt: skip
            return self._append(entry)

    def _append(self, entry: Entry) -> Entry:
        try:
            self.store.append(entry)
        except OSError as exc:
            raise EridiError(
                f"Could not write to the record folder {self.store.folder}: "
                f"{exc.strerror or exc}. Check that it's reachable and writable."
            ) from None
        sync_records(self.conn, self.store, full=False)
        self._last_refresh = time.monotonic()
        return entry

    # --- reading the record ---

    def _all_entries(self) -> list[Entry]:
        return [
            parse_entry(json.loads(r[0])) for r in self.conn.execute("SELECT json FROM entries")
        ]

    def _get_entry(self, entry_id: str) -> Entry | None:
        if not is_ulid(entry_id):
            return None
        row = self.conn.execute("SELECT json FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is not None:
            return parse_entry(json.loads(row[0]))
        try:
            return self.store.get(entry_id)
        except EridiError:
            return None

    def get_entry(self, entry_id: str) -> Entry:
        with self._lock:
            self._refresh_records()
            entry = self._get_entry(entry_id.strip().upper())
            if entry is None:
                raise UnknownEntryError(entry_id)
            return entry

    def entry_state(self, entry_id: str) -> str | None:
        row = self.conn.execute("SELECT state FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return None if row is None else str(row[0])

    def recent(
        self, limit: int = 20, kind: str | None = None, project: str | None = None
    ) -> list[Entry]:
        """Current entries, newest first."""
        with self._lock:
            self._refresh_records()
            sql = "SELECT json FROM entries WHERE state = 'current' AND kind != 'tombstone'"
            params: list[object] = []
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            if project is not None:
                pid = self._get_project(project).id
                sql += " AND " + entry_in_project_sql("entries")
                params += [pid, pid]
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [parse_entry(json.loads(r[0])) for r in self.conn.execute(sql, params)]

    def file_history(self, ref: str) -> FileHistory:
        """A file's text versions, moves, and every entry about it (including corrections,
        retractions and outcomes), oldest first."""
        with self._lock:
            self._refresh_records()
            file = self._resolve_file(ref)
            assert file.id is not None  # noqa: S101 - files from the index have IDs
            moves = file_moves(self.conn, file.id)
            paths = {file.path, *(m[0] for m in moves)}
            entries = self._all_entries()
            items: dict[str, HistoryItem] = {}
            for path in paths:
                for item in history(entries, file.root_id, path):
                    items[item.entry.id] = item
            versions = [
                VersionInfo(sha, seen, source, self.snapshots.has(sha), sha == file.text_sha256)
                for sha, seen, source in text_versions(self.conn, file.id)
            ]
            return FileHistory(
                file,
                self.root_label(file.root_id),
                versions,
                [items[i] for i in sorted(items)],
                moves,
            )

    # --- health ---

    def doctor(self) -> DoctorReport:
        """Check everything Eridi depends on, and say how to fix what's wrong."""
        report = DoctorReport()
        try:
            check_fts5()
            report.add("Search engine", "ok", "SQLite has full-text search (FTS5).")
        except Exception as exc:
            report.add("Search engine", "error", str(exc))
        if self.initialized:
            report.add("Setup", "ok", f"Writing as {self._config.user.name}.")
        else:
            report.add("Setup", "warning", "Not set up yet. Run `eridi init`.")
        with self._lock:
            try:
                read = self.store.read()
                report.add(
                    "Record folder", "ok",
                    f"{self.store.folder}: {len(read.entries)} entries.",
                )  # fmt: skip
                present = {e.id for e in read.entries}
                for problem in read.problems:
                    original = conflict_original(problem.path)
                    if original is None:
                        report.add(
                            "Record file ignored", "warning", f"{problem.path}: {problem.reason}"
                        )
                    elif original in present:
                        report.add(
                            "Sync conflict copy", "warning",
                            f"{problem.path}: a duplicate your sync app made of entry "
                            f"{original}, which is intact. Eridi ignores it; you can delete it.",
                        )  # fmt: skip
                    else:
                        report.add(
                            "Sync conflict copy", "warning",
                            f"{problem.path}: a copy your sync app made of entry {original}, "
                            f"whose original is missing. Rename it to {original}.json to "
                            "restore the entry.",
                        )  # fmt: skip
                for copy in self.store.marker_copies():
                    report.add(
                        "Sync conflict copy", "warning",
                        f"{copy}: a copy of the record folder's marker. Eridi uses "
                        f"{MARKER_NAME}; you can delete this copy.",
                    )  # fmt: skip
                if read.newer_version:
                    names = ", ".join(p.name for p in read.newer_version[:3])
                    more = "…" if len(read.newer_version) > 3 else ""
                    report.add(
                        "Newer entries", "warning",
                        f"{len(read.newer_version)} entries ({names}{more}) were written by a "
                        "newer version of Eridi, probably by a labmate. Update Eridi to see "
                        "them; until then they're kept but not shown.",
                    )  # fmt: skip
            except (EridiError, OSError) as exc:
                report.add("Record folder", "error", str(exc))
            if not self._config.roots:
                report.add("Storage", "warning", "No storage connected. Run `eridi storage add`.")
            for root in self._config.roots:
                location = Path(root.location)
                if root.kind == "msgraph":
                    report.add(f"Storage {root.label!r}", *self._microsoft_status(root))
                    continue
                if root.kind == "local" and not location.is_dir():
                    report.add(
                        f"Storage {root.label!r}", "error",
                        f"{location} can't be found. Reconnect the drive or remove this storage.",
                    )  # fmt: skip
                else:
                    state = "search on" if root.search else "search off"
                    report.add(f"Storage {root.label!r}", "ok", f"{location} ({state}).")
                    marker = self.root_marker(root.id)
                    if marker is not None and marker != root.id:
                        report.add(
                            f"Storage {root.label!r}", "warning",
                            f"The folder is marked as storage {marker} (by another computer "
                            f"or a labmate), but this computer calls it {root.id}, so notes "
                            "about its files don't line up. Run "
                            f"`eridi storage mark {root.id}` to use the shared ID.",
                        )  # fmt: skip
            counts = dict(
                self.conn.execute(
                    "SELECT extract_status, count(*) FROM files WHERE status = 'present' "
                    "GROUP BY extract_status"
                ).fetchall()
            )
            explain = {
                "too_large": "too large to read (see settings.size_limit_mb)",
                "online_only": "online-only (make them available offline to search their text)",
                "needs_ocr": "scans without a text layer",
                "error": "could not be read",
                "pending": "not read yet (a scan was stopped; run `eridi scan` to finish)",
            }
            for status, words in explain.items():
                if counts.get(status):
                    report.add("Skipped files", "warning", f"{counts[status]} files {words}.")
            used = self.snapshots.total_bytes()
            cap = self.snapshots.cap_bytes
            report.add(
                "Snapshots", "ok" if used <= cap else "warning",
                f"{used / 2**20:.1f} MB of {cap / 2**20:.0f} MB used.",
            )  # fmt: skip
        return report


def _same_bytes(connector: Connector, path: str, job: OutboxJob) -> bool:
    import hashlib

    try:
        return hashlib.sha256(connector.open_bytes(path)).hexdigest() == job.sha256
    except ConnectorError:
        return False


def _mark_baseline(conn: sqlite3.Connection, root_id: str) -> None:
    """After a root's first complete scan, what it found was already there, not "added"."""
    key = BASELINE_PREFIX + root_id
    if get_meta(conn, key) is None:
        with conn:
            set_meta(conn, key, utc_now())
