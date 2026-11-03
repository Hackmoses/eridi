"""Live watching: notice edits a couple of seconds after a file is saved.

While the app (or `eridi watch`) runs, every local root with search on is watched, and so
is the record folder, so labmates' notes appear as soon as their sync app delivers them.
Saves are collected for `DEBOUNCE_SECONDS` after the last event (editors save in several
steps), then only those files are scanned. Folder moves and anything the watcher can't
pin to a file trigger a scan of that root. Periodic full scans still run, to catch
anything a watcher misses (sleep, network drives, events dropped by the OS).

If a folder can't be watched (too many folders for the OS, an unsupported network drive),
it is checked every `POLL_SECONDS` instead.
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from eridi.core.models import Entry
from eridi.core.record.store import ENTRY_FILE_RE
from eridi.core.scan import ScanReport
from eridi.core.service import Eridi, EridiError

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0
RESYNC_SECONDS = 10.0
"""How often the set of watched folders is checked against the connected storage."""
POLL_SECONDS = 60.0
"""How often folders that can't be watched are checked instead."""
RECORD = "record"
"""The key for the record folder among watched folders."""

OnReport = Callable[[str, ScanReport], None]
OnEntries = Callable[[list[Entry]], None]


class _RecordCollector(FileSystemEventHandler):
    """Notices new entry files in the record folder."""

    def __init__(self, watcher: "Watcher") -> None:
        self.watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type in ("opened", "closed_no_write", "deleted"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            name = Path(raw.decode() if isinstance(raw, bytes) else raw).name if raw else ""
            if ENTRY_FILE_RE.match(name):
                self.watcher.note_record()
                return


class _Collector(FileSystemEventHandler):
    """Collects the root-relative paths of changed files for one root."""

    def __init__(self, watcher: "Watcher", root_id: str, location: Path) -> None:
        self.watcher = watcher
        self.root_id = root_id
        self.location = location

    def _relative(self, raw: str | bytes) -> str | None:
        path = Path(raw.decode() if isinstance(raw, bytes) else raw)
        try:
            return path.relative_to(self.location).as_posix()
        except ValueError:
            return None

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        if event.is_directory:
            if event.event_type in ("moved", "deleted", "created"):
                self.watcher.note(self.root_id, None)  # a whole folder: rescan the root
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if raw:
                rel = self._relative(raw)
                if rel and rel != ".":
                    self.watcher.note(self.root_id, rel)


class Watcher:
    def __init__(
        self,
        eridi: Eridi,
        *,
        debounce: float = DEBOUNCE_SECONDS,
        on_report: OnReport | None = None,
        on_entries: OnEntries | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.eridi = eridi
        self.debounce = debounce
        self.on_report = on_report
        self.on_entries = on_entries
        self.poll_seconds = poll_seconds
        self._observer = Observer()
        self._observer_running = False
        self._watches: dict[str, tuple[str, ObservedWatch]] = {}
        self.polled: dict[str, str] = {}
        """Folders checked every `poll_seconds` because watching failed: key → reason.
        Keys are root IDs, or `RECORD` for the record folder."""
        self._last_poll = time.monotonic()
        self._record_dirty = False
        self._seen_entry: str | None = None
        """The newest entry already accounted for (arrivals after it are reported)."""
        self._pending: dict[str, set[str]] = {}
        self._full: set[str] = set()
        self._last_event = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def watching(self) -> list[str]:
        """Root IDs being watched (and `RECORD` when the record folder is)."""
        return sorted(self._watches)

    def note_record(self) -> None:
        """Something arrived in the record folder."""
        with self._lock:
            self._record_dirty = True
            self._last_event = time.monotonic()

    def note(self, root_id: str, path: str | None) -> None:
        """Record a change (`path=None`: something changed, rescan the whole root)."""
        with self._lock:
            if path is None:
                self._full.add(root_id)
            else:
                self._pending.setdefault(root_id, set()).add(path)
            self._last_event = time.monotonic()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._seen_entry = self.eridi.latest_entry_id()
        try:
            self._observer.start()
            self._observer_running = True
        except Exception as exc:  # e.g. the OS refuses another file watcher
            log.warning("Can't watch folders (%s); checking them every minute instead.", exc)
            self._observer_failed = str(exc)
        self._sync_watches()
        self._thread = threading.Thread(target=self._loop, name="eridi-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self._observer_running:
            self._observer.stop()
            self._observer.join(timeout=10)

    _observer_failed: str | None = None

    def _wanted(self) -> dict[str, str]:
        wanted = {
            r.id: r.location
            for r in self.eridi.list_roots()
            if r.kind == "local" and r.search and Path(r.location).is_dir()
        }
        try:
            entries = self.eridi.store.entries_dir
        except EridiError:
            return wanted
        if entries.is_dir():
            wanted[RECORD] = str(entries)
        return wanted

    def _sync_watches(self) -> None:
        """Watch exactly the local roots with search on and the record folder (storage can
        change while running)."""
        wanted = self._wanted()
        for key, (location, watch) in list(self._watches.items()):
            if wanted.get(key) != location:
                self._observer.unschedule(watch)
                del self._watches[key]
        for key in list(self.polled):
            if key not in wanted:
                del self.polled[key]
        for key, location in wanted.items():
            if key in self._watches:
                continue
            if self._observer_failed is not None:
                self.polled[key] = self._observer_failed
                continue
            try:
                handler: FileSystemEventHandler = (
                    _RecordCollector(self)
                    if key == RECORD
                    else _Collector(self, key, Path(location).resolve())
                )
                watch = self._observer.schedule(handler, location, recursive=True)
            except Exception as exc:  # OSError, or the OS's limit on watched folders
                if key not in self.polled:
                    log.warning("Can't watch %s (%s); checking it every minute instead.",
                                location, exc)  # fmt: skip
                self.polled[key] = str(exc) or type(exc).__name__
                continue
            self.polled.pop(key, None)
            self._watches[key] = (location, watch)

    def _poll(self) -> None:
        """Treat every folder that can't be watched as changed. Unlike a real event, this
        doesn't restart the quiet period, so it's handled on the next tick."""
        with self._lock:
            for key in list(self.polled):
                if key == RECORD:
                    self._record_dirty = True
                else:
                    self._full.add(key)

    def flush(self) -> list[tuple[str, ScanReport]]:
        """Scan whatever has been collected now (also used by tests)."""
        with self._lock:
            pending, self._pending = self._pending, {}
            full, self._full = self._full, set()
            record, self._record_dirty = self._record_dirty, False
        if record:
            arrived = self.eridi.arrivals_after(self._seen_entry)
            if arrived:
                self._seen_entry = arrived[-1].id
            if arrived and self.on_entries is not None:
                self.on_entries(arrived)
        reports: list[tuple[str, ScanReport]] = []
        for root_id in full:
            pending.pop(root_id, None)
            summary = self.eridi.scan(root_id)
            reports += [(root_id, r) for r in summary.reports]
        for root_id, paths in pending.items():
            report = self.eridi.scan_changed(root_id, paths)
            if report is None:  # a full scan was running; try again shortly
                with self._lock:
                    self._pending.setdefault(root_id, set()).update(paths)
                continue
            reports.append((root_id, report))
        for root_id, report in reports:
            if self.on_report is not None and (
                report.new or report.changed or report.moved or report.missing
            ):
                self.on_report(root_id, report)
        return reports

    def _loop(self) -> None:
        last_sync = time.monotonic()
        while not self._stop.wait(0.5):
            try:
                if time.monotonic() - last_sync > RESYNC_SECONDS:
                    self._sync_watches()
                    last_sync = time.monotonic()
                if self.polled and time.monotonic() - self._last_poll >= self.poll_seconds:
                    self._last_poll = time.monotonic()
                    self._poll()
                with self._lock:
                    waiting = bool(self._pending or self._full or self._record_dirty)
                    quiet = time.monotonic() - self._last_event >= self.debounce
                if waiting and quiet:
                    self.flush()
            except Exception:
                log.exception("Watching for changes failed; carrying on")
