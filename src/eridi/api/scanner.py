"""Background work while the app is open: scans every N minutes (or right away when asked),
and uploads of files saved through the app, retried until they land."""

import logging
import threading
from dataclasses import dataclass
from typing import Any

from eridi.core.models import utc_now
from eridi.core.service import Eridi, ScanSummary

log = logging.getLogger(__name__)

OUTBOX_CHECK_S = 15.0
"""How often waiting uploads are looked at (each job also waits its own retry delay)."""


@dataclass
class ScanStatus:
    running: bool = False
    last_started: str | None = None
    last_finished: str | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None


def summary_json(eridi: Eridi, summary: ScanSummary) -> dict[str, Any]:
    return {
        "rebuilt": summary.rebuilt,
        "storage": [
            {
                "id": r.root_id,
                "label": eridi.root_label(r.root_id),
                "new": len(r.new),
                "changed": len(r.changed),
                "unchanged": r.unchanged,
                "moved": len(r.moved),
                "missing": len(r.missing),
                "skipped": len(r.skipped),
                "errors": [{"path": p, "reason": why} for p, why in r.errors],
                "unavailable": r.unavailable,
            }
            for r in summary.reports
        ],
    }


class BackgroundScanner:
    def __init__(
        self, eridi: Eridi, interval_minutes: float, *, scan_on_start: bool = True
    ) -> None:
        """`interval_minutes <= 0` turns timed scans off; scans then run only on request."""
        self.eridi = eridi
        self.interval = interval_minutes * 60 if interval_minutes > 0 else None
        self.scan_on_start = scan_on_start
        self.status = ScanStatus()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._count = 0
        self._outbox_thread: threading.Thread | None = None
        self._outbox_wake = threading.Event()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="eridi-scan", daemon=True)
            self._thread.start()
            if self.interval is not None and self.scan_on_start:
                self._wake.set()  # scan once on start, then every interval
        if self._outbox_thread is None:
            self._outbox_thread = threading.Thread(
                target=self._outbox_loop, name="eridi-outbox", daemon=True
            )
            self._outbox_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._outbox_wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        if self._outbox_thread is not None:
            self._outbox_thread.join(timeout=30)
            self._outbox_thread = None

    def upload_soon(self) -> None:
        """Look at waiting uploads now (e.g. after the user retried one)."""
        self._outbox_wake.set()

    def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.eridi.process_outbox()
            except Exception:
                log.exception("Uploading saved files failed; will try again")
            self._outbox_wake.wait(timeout=OUTBOX_CHECK_S)
            self._outbox_wake.clear()

    def request(self) -> bool:
        """Ask for a scan soon. False if one is already running."""
        if self.status.running:
            return False
        self._wake.set()
        return True

    def run_now(self) -> ScanStatus:
        """Scan in the calling thread (waits for a running scan to finish first)."""
        with self._run_lock:
            self.status.running = True
            self.status.last_started = utc_now()
            try:
                summary = self.eridi.scan()
                self.status.last_summary = summary_json(self.eridi, summary)
                self.status.last_error = None
            except Exception as exc:
                log.exception("Background scan failed")
                self.status.last_error = str(exc)
            finally:
                self.status.running = False
                self.status.last_finished = utc_now()
        return self.status

    @property
    def scans(self) -> int:
        return self._count

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if self.eridi.list_roots():
                self.run_now()
                self._count += 1
