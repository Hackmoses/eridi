"""Live watching: notice edits a couple of seconds after a file is saved.

While the app (or `eridi watch`) runs, every local root with search on is watched. Saves
are collected for `DEBOUNCE_SECONDS` after the last event (editors save in several
steps), then only those files are scanned. Folder moves and anything the watcher can't
pin to a file trigger a scan of that root. Periodic full scans still run, to catch
anything a watcher misses (sleep, network drives, events dropped by the OS).
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from eridi.core.scan import ScanReport
from eridi.core.service import Eridi

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0
RESYNC_SECONDS = 10.0
"""How often the set of watched folders is checked against the connected storage."""

OnReport = Callable[[str, ScanReport], None]


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
    ) -> None:
        self.eridi = eridi
        self.debounce = debounce
        self.on_report = on_report
        self._observer = Observer()
        self._watches: dict[str, tuple[str, ObservedWatch]] = {}
        self._pending: dict[str, set[str]] = {}
        self._full: set[str] = set()
        self._last_event = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def watching(self) -> list[str]:
        """Root IDs being watched."""
        return sorted(self._watches)

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
        self._sync_watches()
        self._observer.start()
        self._thread = threading.Thread(target=self._loop, name="eridi-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._observer.stop()
        self._observer.join(timeout=10)

    def _sync_watches(self) -> None:
        """Watch exactly the local roots with search on (storage can change while running)."""
        wanted = {
            r.id: r.location
            for r in self.eridi.list_roots()
            if r.kind == "local" and r.search and Path(r.location).is_dir()
        }
        for root_id, (location, watch) in list(self._watches.items()):
            if wanted.get(root_id) != location:
                self._observer.unschedule(watch)
                del self._watches[root_id]
        for root_id, location in wanted.items():
            if root_id in self._watches:
                continue
            try:
                handler = _Collector(self, root_id, Path(location).resolve())
                watch = self._observer.schedule(handler, location, recursive=True)
            except OSError as exc:
                log.warning("Can't watch %s: %s", location, exc)
                continue
            self._watches[root_id] = (location, watch)

    def flush(self) -> list[tuple[str, ScanReport]]:
        """Scan whatever has been collected now (also used by tests)."""
        with self._lock:
            pending, self._pending = self._pending, {}
            full, self._full = self._full, set()
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
                with self._lock:
                    waiting = bool(self._pending or self._full)
                    quiet = time.monotonic() - self._last_event >= self.debounce
                if waiting and quiet:
                    self.flush()
            except Exception:
                log.exception("Watching for changes failed; carrying on")
