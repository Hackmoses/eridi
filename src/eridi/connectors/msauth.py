"""Signing in to Microsoft (OneDrive and SharePoint) with MSAL, read-only.

Eridi is a "public client": it has no secret, so nothing on a server can sign in as it.
Sign-in uses the authorization code flow with PKCE and a loopback redirect
(`http://localhost`, opened in the user's browser); the device-code flow is the fallback
for computers where a browser can't be opened. Scopes are read-only.

Tokens are kept by MSAL in a token cache that Eridi stores in the OS keychain (`keyring`),
never in a file or a log. Windows' Credential Manager limits each entry's size, so the
cache is split across several entries.
"""

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import keyring
import msal

from eridi.connectors.base import ConnectorError

SCOPES = ["Files.Read.All", "Sites.Read.All", "User.Read"]
"""Read-only. MSAL adds `offline_access` (to stay signed in), `openid` and `profile`."""
AUTHORITY = "https://login.microsoftonline.com/common"
"""Work and school accounts from any organization, and personal Microsoft accounts."""
ENV_CLIENT_ID = "ERIDI_MS_CLIENT_ID"
DEFAULT_CLIENT_ID = ""
"""Eridi's multi-tenant app registration in Microsoft Entra (public client, no secret).
Empty until the project registers it; see docs/MICROSOFT.md."""

KEYRING_SERVICE = "eridi-microsoft"
CHUNK = 1200
"""Characters per keychain entry (Windows Credential Manager allows about 2,500 bytes)."""

# Microsoft's error codes that mean an organization must approve the app first.
_APPROVAL_CODES = ("AADSTS65001", "AADSTS90094", "AADSTS90093", "AADSTS50105", "AADSTS65004")
IT_APPROVAL_DOC = "docs/IT_APPROVAL.md"


class MicrosoftAuthError(ConnectorError):
    """Signing in to Microsoft didn't work. The message says what to do."""


class SignInNeededError(MicrosoftAuthError):
    def __init__(self, who: str = "") -> None:
        account = f" as {who}" if who else ""
        super().__init__(
            f"Eridi is no longer signed in to Microsoft{account}. "
            "Sign in again with `eridi storage add-microsoft`."
        )


class ApprovalRequiredError(MicrosoftAuthError):
    """The user's organization (e.g. university IT) must approve Eridi before it can read."""

    def __init__(self, consent_url: str, detail: str = "") -> None:
        self.consent_url = consent_url
        self.detail = detail
        super().__init__(
            "Your university needs to approve this app. Send IT this link: "
            f"{consent_url} (what Eridi reads and why: {IT_APPROVAL_DOC})."
        )


@dataclass(frozen=True)
class MicrosoftAccount:
    id: str
    """MSAL's home account ID; stored as the storage's `account`."""
    username: str
    tenant_id: str | None = None


def client_id(configured: str = "") -> str:
    """The app ID to sign in with: the environment, then the setting, then Eridi's own."""
    value = os.environ.get(ENV_CLIENT_ID) or configured or DEFAULT_CLIENT_ID
    if not value:
        raise MicrosoftAuthError(
            "This copy of Eridi has no Microsoft app ID yet, so it can't sign in to "
            "OneDrive or SharePoint. Set `settings.microsoft_client_id` in config.toml (or the "
            f"{ENV_CLIENT_ID} environment variable) to an app registered as described in "
            "docs/MICROSOFT.md."
        )
    return value


# --- the token cache, in the OS keychain ---


class TokenStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, text: str) -> None: ...


class KeyringTokenStore:
    """MSAL's serialized cache, split across keychain entries `cache:0`, `cache:1`, …"""

    def __init__(self, client: str) -> None:
        self.prefix = f"cache:{client}"

    def load(self) -> str | None:
        count = keyring.get_password(KEYRING_SERVICE, f"{self.prefix}:count")
        if not count or not count.isdigit():
            return None
        parts = [
            keyring.get_password(KEYRING_SERVICE, f"{self.prefix}:{i}") for i in range(int(count))
        ]
        if any(p is None for p in parts):
            return None  # half-written: start over rather than guess
        return "".join(p for p in parts if p is not None)

    def save(self, text: str) -> None:
        chunks = [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
        old = keyring.get_password(KEYRING_SERVICE, f"{self.prefix}:count")
        for i, chunk in enumerate(chunks):
            keyring.set_password(KEYRING_SERVICE, f"{self.prefix}:{i}", chunk)
        keyring.set_password(KEYRING_SERVICE, f"{self.prefix}:count", str(len(chunks)))
        if old and old.isdigit():  # blank leftovers from a longer cache
            for i in range(len(chunks), int(old)):
                keyring.set_password(KEYRING_SERVICE, f"{self.prefix}:{i}", "")


# --- signing in ---

AppFactory = Callable[[str, Any], Any]
"""(client_id, token_cache) -> an MSAL PublicClientApplication (or a test double)."""


def _msal_app(client: str, cache: Any) -> Any:
    return msal.PublicClientApplication(client, authority=AUTHORITY, token_cache=cache)


def _tenant_of(result: dict[str, Any]) -> str | None:
    claims = result.get("id_token_claims") or {}
    tid = claims.get("tid")
    return tid if isinstance(tid, str) else None


class MicrosoftAuth:
    def __init__(
        self,
        configured_client_id: str = "",
        *,
        store: TokenStore | None = None,
        app_factory: AppFactory = _msal_app,
    ) -> None:
        self.client_id = client_id(configured_client_id)
        self.store: TokenStore = store or KeyringTokenStore(self.client_id)
        self.cache = msal.SerializableTokenCache()
        saved = self.store.load()
        if saved:
            with contextlib.suppress(ValueError):  # unreadable: as if signed out
                self.cache.deserialize(saved)
        self.app = app_factory(self.client_id, self.cache)

    def _persist(self) -> None:
        if self.cache.has_state_changed:
            self.store.save(self.cache.serialize())

    def consent_url(self, tenant: str | None = None) -> str:
        return (
            f"https://login.microsoftonline.com/{tenant or 'organizations'}/adminconsent"
            f"?client_id={self.client_id}"
        )

    def _check(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if result and "access_token" in result:
            self._persist()
            return result
        result = result or {}
        description = str(result.get("error_description") or result.get("error") or "")
        if result.get("error") == "consent_required" or any(
            code in description for code in _APPROVAL_CODES
        ):
            raise ApprovalRequiredError(self.consent_url(_tenant_of(result)), description)
        if result.get("error") in ("access_denied", "authorization_declined"):
            raise MicrosoftAuthError("Sign-in was cancelled. Try again when you're ready.")
        first_line = description.splitlines()[0] if description else "no reason given"
        raise MicrosoftAuthError(f"Microsoft sign-in didn't work: {first_line}")

    def _account(self, result: dict[str, Any]) -> MicrosoftAccount:
        claims = result.get("id_token_claims") or {}
        username = str(claims.get("preferred_username") or claims.get("email") or "")
        for account in self.app.get_accounts():
            if not username or account.get("username", "").lower() == username.lower():
                return MicrosoftAccount(
                    account["home_account_id"], account.get("username", username),
                    _tenant_of(result),
                )  # fmt: skip
        raise MicrosoftAuthError("Signed in, but Microsoft returned no account. Try again.")

    def sign_in(
        self,
        *,
        device_code: bool = False,
        show: Callable[[str], None] = print,
        timeout: int = 300,
    ) -> MicrosoftAccount:
        """Sign in in the browser, or with a code typed at microsoft.com/devicelogin."""
        if device_code:
            flow = self.app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                self._check(flow)
            show(str(flow["message"]))
            result = self.app.acquire_token_by_device_flow(flow)
        else:
            try:
                result = self.app.acquire_token_interactive(
                    SCOPES, prompt="select_account", timeout=timeout
                )
            except Exception as exc:  # no browser, or the loopback port can't be opened
                raise MicrosoftAuthError(
                    f"Couldn't open the Microsoft sign-in page ({exc}). Sign in with a code "
                    "instead: `eridi storage add-microsoft --device-code`."
                ) from None
        return self._account(self._check(result))

    def accounts(self) -> list[MicrosoftAccount]:
        return [
            MicrosoftAccount(a["home_account_id"], a.get("username", ""))
            for a in self.app.get_accounts()
        ]

    def token(self, account_id: str) -> str:
        """A current access token for the account, refreshed silently when needed."""
        account = next(
            (a for a in self.app.get_accounts() if a.get("home_account_id") == account_id), None
        )
        if account is None:
            raise SignInNeededError()
        result = self.app.acquire_token_silent(SCOPES, account=account)
        if not result:
            raise SignInNeededError(account.get("username", ""))
        if "access_token" not in result:
            description = str(result.get("error_description") or "")
            if any(code in description for code in _APPROVAL_CODES):
                raise ApprovalRequiredError(self.consent_url(), description)
            raise SignInNeededError(account.get("username", ""))
        self._persist()
        return str(result["access_token"])
