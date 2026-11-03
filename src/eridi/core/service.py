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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from eridi.connectors import connector_for
from eridi.connectors.base import Connector, ConnectorError
from eridi.connectors.local import read_root_marker, write_root_marker
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
from eridi.core.index.db import check_fts5, get_meta, index_path, open_index, set_meta
from eridi.core.index.files import (
    file_moves,
    get_file,
    list_files,
    restore_versions,
    text_versions,
    upsert_root,
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
    Root,
    Subject,
    utc_now,
)
from eridi.core.paths import PathOutsideRootError, app_home, to_posix_relative
from eridi.core.record.schema import make_entry, parse_entry
from eridi.core.record.store import RecordFolderError, RecordStore
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
        self._last_refresh = 0.0
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
        self, name: str, email: str | None = None, record_folder: Path | None = None
    ) -> Config:
        """Set who is writing, and where the record lives (default `<app-data>/record`)."""
        if not name.strip():
            raise EridiError("A name is needed, so entries show who wrote them.")
        with self._lock:
            folder = record_folder.expanduser().resolve() if record_folder else None
            config = replace(
                self._config, user=User(name.strip(), (email or "").strip() or None),
                record_folder=folder,
            )  # fmt: skip
            try:
                self._store = RecordStore.init(config.effective_record_folder)
            except OSError as exc:
                raise EridiError(
                    f"Could not create the record folder {config.effective_record_folder}: "
                    f"{exc.strerror or exc}. Choose a folder you can write to."
                ) from None
            self._save(config)
            self._last_refresh = 0.0
            return config

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

    def connector(self, root: Root) -> Connector:
        return connector_for(root, self._config.settings, ignore=self._own_folders_ignored(root))

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
            hits = search(self.conn, query, limit, project)
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
        root_id, sep, rest = ref.partition(":")
        if sep and self._config.root(root_id) is not None:
            found = get_file(self.conn, root_id, rest)
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
                self.conn, self.snapshots, self._config.dismissed_changes, project
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
            if file is not None:
                info = self._resolve_file(file)
                subjects.append(self._subject(info))
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
                project_id=project,
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
                sql += " AND project_id = ?"
                params.append(project)
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
                for problem in read.problems:
                    report.add(
                        "Record file ignored", "warning", f"{problem.path}: {problem.reason}"
                    )
                if read.newer_version:
                    report.add(
                        "Newer entries", "warning",
                        f"{len(read.newer_version)} entries were written by a newer version of "
                        "Eridi. Update to see them.",
                    )  # fmt: skip
            except (EridiError, OSError) as exc:
                report.add("Record folder", "error", str(exc))
            if not self._config.roots:
                report.add("Storage", "warning", "No storage connected. Run `eridi storage add`.")
            for root in self._config.roots:
                location = Path(root.location)
                if root.kind == "local" and not location.is_dir():
                    report.add(
                        f"Storage {root.label!r}", "error",
                        f"{location} can't be found. Reconnect the drive or remove this storage.",
                    )  # fmt: skip
                else:
                    state = "search on" if root.search else "search off"
                    report.add(f"Storage {root.label!r}", "ok", f"{location} ({state}).")
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


def _mark_baseline(conn: sqlite3.Connection, root_id: str) -> None:
    """After a root's first complete scan, what it found was already there, not "added"."""
    key = BASELINE_PREFIX + root_id
    if get_meta(conn, key) is None:
        with conn:
            set_meta(conn, key, utc_now())
