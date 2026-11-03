"""The web app's "Add Google Drive" flow: sign-in runs in a background thread (it waits for
the user in their browser) while the page polls its state."""

import threading
from dataclasses import dataclass, field
from typing import Any

from eridi.connectors.base import ConnectorError
from eridi.connectors.gdrive import DriveInfo, root_id_for
from eridi.core.config import ConfigError
from eridi.core.service import Eridi, EridiError


@dataclass
class GoogleSignInState:
    state: str = "idle"
    """"idle", "waiting", "done" or "error"."""
    message: str | None = None
    account_id: str | None = None
    email: str | None = None
    drives: list[DriveInfo] = field(default_factory=list)


class GoogleSignIn:
    def __init__(self, eridi: Eridi) -> None:
        self.eridi = eridi
        self.status = GoogleSignInState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start signing in. False if a sign-in is already waiting."""
        with self._lock:
            if self.status.state == "waiting":
                return False
            self.status = GoogleSignInState("waiting")
        self._thread = threading.Thread(target=self._run, name="eridi-google-sign-in",
                                        daemon=True)  # fmt: skip
        self._thread.start()
        return True

    def wait(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _show(self, message: str) -> None:
        with self._lock:
            self.status.message = message

    def _run(self) -> None:
        try:
            account, drives = self.eridi.google_sign_in(show=self._show)
        except (ConnectorError, EridiError, ConfigError) as exc:
            with self._lock:
                self.status = GoogleSignInState("error", str(exc))
            return
        except Exception as exc:  # never leave the page waiting forever
            with self._lock:
                self.status = GoogleSignInState("error", f"Sign-in failed unexpectedly: {exc}")
            return
        with self._lock:
            self.status = GoogleSignInState(
                "done", account_id=account.id, email=account.email, drives=drives
            )

    def as_json(self) -> dict[str, Any]:
        with self._lock:
            s = self.status
            connected = {r.id for r in self.eridi.list_roots()}
            return {
                "state": s.state,
                "message": s.message,
                "account_id": s.account_id,
                "email": s.email,
                "drives": [
                    {
                        "id": d.id,
                        "label": d.label,
                        "kind": d.kind,
                        "connected": root_id_for(d.id) in connected,
                    }
                    for d in s.drives
                ],
            }

    def drive(self, drive_id: str) -> DriveInfo | None:
        with self._lock:
            return next((d for d in self.status.drives if d.id == drive_id), None)
