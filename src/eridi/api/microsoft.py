"""The web app's "Add Microsoft storage" flow.

Signing in can take minutes (the user types a password, maybe a second factor), so it runs
in a background thread; the page polls its state. The Microsoft sign-in page opens in the
user's browser, on this computer, exactly as from the terminal.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from eridi.connectors.base import ConnectorError
from eridi.connectors.msauth import ApprovalRequiredError
from eridi.connectors.msgraph import DriveInfo, root_id_for
from eridi.core.config import ConfigError
from eridi.core.service import Eridi, EridiError


@dataclass
class SignInState:
    state: str = "idle"
    """"idle", "waiting", "done", "approval" (IT must approve) or "error"."""
    message: str | None = None
    """Device-code instructions while waiting, or what went wrong."""
    consent_url: str | None = None
    account_id: str | None = None
    username: str | None = None
    drives: list[DriveInfo] = field(default_factory=list)


class MicrosoftSignIn:
    def __init__(self, eridi: Eridi) -> None:
        self.eridi = eridi
        self.status = SignInState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, *, device_code: bool = False, allow_save: str | None = None) -> bool:
        """Start signing in (or, with `allow_save=<storage ID>`, approving saving there).
        False if a sign-in is already waiting."""
        with self._lock:
            if self.status.state == "waiting":
                return False
            self.status = SignInState("waiting")
        self._thread = threading.Thread(
            target=self._run,
            args=(device_code, allow_save),
            name="eridi-ms-sign-in",
            daemon=True,
        )
        self._thread.start()
        return True

    def wait(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _show(self, message: str) -> None:
        with self._lock:
            self.status.message = message

    def _run(self, device_code: bool, allow_save: str | None = None) -> None:
        try:
            if allow_save is not None:
                self.eridi.allow_microsoft_saving(
                    allow_save, device_code=device_code, show=self._show
                )
                with self._lock:
                    self.status = SignInState("done", message="Saving is on.")
                return
            account, drives = self.eridi.microsoft_sign_in(device_code=device_code, show=self._show)
        except ApprovalRequiredError as exc:
            with self._lock:
                self.status = SignInState("approval", str(exc), exc.consent_url)
            return
        except (ConnectorError, EridiError, ConfigError) as exc:
            with self._lock:
                self.status = SignInState("error", str(exc))
            return
        except Exception as exc:  # never leave the page waiting forever
            with self._lock:
                self.status = SignInState("error", f"Sign-in failed unexpectedly: {exc}")
            return
        with self._lock:
            self.status = SignInState(
                "done", account_id=account.id, username=account.username, drives=drives
            )

    def as_json(self) -> dict[str, Any]:
        with self._lock:
            s = self.status
            connected = {r.id for r in self.eridi.list_roots()}
            return {
                "state": s.state,
                "message": s.message,
                "consent_url": s.consent_url,
                "account_id": s.account_id,
                "username": s.username,
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
