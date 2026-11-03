"""Signing in to Google Drive, read-only.

Eridi is a Google "desktop app" OAuth client. Sign-in opens the user's browser and
receives the answer on a loopback address (`http://localhost:<port>`), via
`google-auth-oauthlib`. The one scope is `drive.readonly`.

`drive.readonly` is a *restricted* scope. While Eridi's Google Cloud project is in
"testing" mode, up to 100 named test users can sign in without Google's verification,
which is enough for pilot labs; anyone else sees "access blocked". Public release needs
Google's verification and security assessment.

Google calls a desktop app's client secret "not confidential" (it ships inside every copy of
the app). The user's tokens are what matter: they are kept in the OS keychain (`keyring`),
never in a file or a log.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eridi.connectors.base import ConnectorError
from eridi.connectors.msauth import KeyringTokenStore, TokenStore

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
ENV_CLIENT_ID = "ERIDI_GOOGLE_CLIENT_ID"
ENV_CLIENT_SECRET = "ERIDI_GOOGLE_CLIENT_SECRET"  # noqa: S105 - the variable's name
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""
"""Eridi's own Google desktop-app client. Empty until the project creates it; see
docs/GOOGLE.md."""
KEYRING_SERVICE = "eridi-google"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
EXPIRY_MARGIN_S = 120


class GoogleAuthError(ConnectorError):
    """Signing in to Google didn't work. The message says what to do."""


class GoogleSignInNeededError(GoogleAuthError):
    def __init__(self, who: str = "") -> None:
        account = f" as {who}" if who else ""
        super().__init__(
            f"Eridi is no longer signed in to Google{account}. Sign in again with "
            "`eridi storage add-google`."
        )


@dataclass(frozen=True)
class GoogleAccount:
    id: str
    """Google's permission ID for the user (stable; stored as the storage's `account`)."""
    email: str


def client_config(configured_id: str = "", configured_secret: str = "") -> dict[str, Any]:
    """The OAuth client: the environment, then the settings, then Eridi's own."""
    cid = os.environ.get(ENV_CLIENT_ID) or configured_id or DEFAULT_CLIENT_ID
    secret = os.environ.get(ENV_CLIENT_SECRET) or configured_secret or DEFAULT_CLIENT_SECRET
    if not cid:
        raise GoogleAuthError(
            "This copy of Eridi has no Google app ID yet, so it can't sign in to Google "
            "Drive. Set `settings.google_client_id` and `settings.google_client_secret` in "
            f"config.toml (or {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET}) to a desktop-app "
            "client created as described in docs/GOOGLE.md."
        )
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


Credentials = dict[str, Any]
"""`token`, `refresh_token`, `expiry` (epoch seconds), `client_id`, `client_secret`."""
FlowRunner = Callable[[dict[str, Any], list[str], Callable[[str], None]], Credentials]
Refresher = Callable[[Credentials], Credentials]


def _run_browser_flow(
    config: dict[str, Any], scopes: list[str], show: Callable[[str], None]
) -> Credentials:  # pragma: no cover - opens a browser; covered by the live test
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes)
    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            authorization_prompt_message="Sign in to Google in your browser: {url}",
            success_message="Signed in. You can close this window and return to Eridi.",
            timeout_seconds=300,
        )
    except Exception as exc:
        text = str(exc)
        if "access_denied" in text:
            raise GoogleAuthError(
                "Google refused the sign-in. While Eridi's Google app is in testing, only "
                "listed test users can use it: ask the project to add your Google account."
            ) from None
        raise GoogleAuthError(f"Google sign-in didn't work: {text.splitlines()[0]}") from None
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "expiry": creds.expiry.timestamp() if creds.expiry else 0.0,
    }


def _refresh(creds: Credentials) -> Credentials:  # pragma: no cover - network
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as GoogleCredentials

    google_creds = GoogleCredentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=creds["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        scopes=SCOPES,
    )
    try:
        google_creds.refresh(Request())  # type: ignore[no-untyped-call]
    except RefreshError:
        raise GoogleSignInNeededError() from None
    except Exception as exc:
        raise GoogleAuthError(f"Couldn't reach Google to refresh sign-in ({exc}).") from None
    expiry = google_creds.expiry
    return {**creds, "token": google_creds.token, "expiry": expiry.timestamp() if expiry else 0.0}


class GoogleAuth:
    def __init__(
        self,
        configured_id: str = "",
        configured_secret: str = "",
        *,
        store: Callable[[str], TokenStore] | None = None,
        run_flow: FlowRunner = _run_browser_flow,
        refresh: Refresher = _refresh,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = client_config(configured_id, configured_secret)
        self.client_id = str(self.config["installed"]["client_id"])
        self._store = store or (
            lambda name: KeyringTokenStore(self.client_id, service=KEYRING_SERVICE, name=name)
        )
        self._run_flow = run_flow
        self._refresh = refresh
        self.clock = clock

    def _load(self, name: str) -> Any:
        text = self._store(name).load()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def _save(self, name: str, value: Any) -> None:
        self._store(name).save(json.dumps(value))

    def accounts(self) -> list[GoogleAccount]:
        data = self._load("accounts") or []
        return [GoogleAccount(a["id"], a["email"]) for a in data if isinstance(a, dict)]

    def sign_in(
        self, identify: Callable[[str], GoogleAccount], *, show: Callable[[str], None] = print
    ) -> GoogleAccount:
        """Sign in in the browser. `identify` turns a fresh access token into the account
        (Drive's `about` call), so the tokens are stored under a stable ID."""
        creds = self._run_flow(self.config, SCOPES, show)
        if not creds.get("refresh_token"):
            raise GoogleAuthError("Google didn't let Eridi stay signed in. Try signing in again.")
        installed = self.config["installed"]
        creds = {**creds, "client_id": installed["client_id"],
                 "client_secret": installed["client_secret"]}  # fmt: skip
        account = identify(str(creds["token"]))
        self._save(f"account-{account.id}", creds)
        others = [a for a in self.accounts() if a.id != account.id]
        self._save("accounts", [{"id": a.id, "email": a.email} for a in [*others, account]])
        return account

    def token(self, account_id: str) -> str:
        """A current access token, refreshed when it is about to expire."""
        creds = self._load(f"account-{account_id}")
        email = next((a.email for a in self.accounts() if a.id == account_id), "")
        if not isinstance(creds, dict) or not creds.get("refresh_token"):
            raise GoogleSignInNeededError(email)
        if creds.get("token") and float(creds.get("expiry") or 0) > self.clock() + EXPIRY_MARGIN_S:
            return str(creds["token"])
        try:
            creds = self._refresh(creds)
        except GoogleSignInNeededError:
            raise GoogleSignInNeededError(email) from None
        self._save(f"account-{account_id}", creds)
        return str(creds["token"])
