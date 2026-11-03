"""Local API security (ARCHITECTURE.md, "Local API security").

A web server on a laptop can be reached by other programs and by web pages in the user's
browser (cross-site requests, DNS rebinding). So every request must:

- name this server in its Host header (`127.0.0.1:<port>` or `localhost:<port>`);
- come from this server's own pages, if it says where it comes from (Origin);
- carry the per-install token, as `Authorization: Bearer <token>` or as the session cookie
  that a one-time link sets.

The token lives in the OS keychain. If no keychain is available, a token is made for this
run only and kept in memory; nothing is ever written to a file.
"""

import hashlib
import hmac
import logging
import secrets
import threading
import time

log = logging.getLogger(__name__)

KEYRING_SERVICE = "Eridi"
KEYRING_USER = "local-api-token"
COOKIE_NAME = "eridi_session"
LINK_SECONDS = 120


def get_or_create_token() -> str:
    """The per-install API token from the OS keychain, created on first use."""
    try:
        import keyring
        from keyring.errors import KeyringError

        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if not token:
                token = secrets.token_urlsafe(32)
                keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
            return token
        except KeyringError as exc:
            log.warning("No OS keychain available (%s); using a token for this run only.", exc)
    except ImportError:  # pragma: no cover - keyring is a dependency
        pass
    return secrets.token_urlsafe(32)


def stored_token() -> str | None:
    """The token if the keychain has one (for `eridi open` talking to a running server)."""
    try:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except KeyringError:
            return None
    except ImportError:  # pragma: no cover
        return None


class Guard:
    """Checks for one running server (one port, one token)."""

    def __init__(self, token: str, port: int) -> None:
        self._token = token
        self.port = port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self._session = hashlib.sha256(f"eridi-session:{token}".encode()).hexdigest()
        self._links: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def session_value(self) -> str:
        return self._session

    def host_ok(self, host: str | None) -> bool:
        return host in self.hosts

    def origin_ok(self, origin: str | None, fetch_site: str | None) -> bool:
        if fetch_site == "cross-site":
            return False
        return origin is None or origin in self.origins

    def authorized(self, authorization: str | None, cookie: str | None) -> bool:
        if authorization and authorization.startswith("Bearer "):
            return hmac.compare_digest(authorization[7:].strip(), self._token)
        return cookie is not None and hmac.compare_digest(cookie, self._session)

    def new_link(self) -> str:
        """A one-time code for `/?t=<code>`, valid for two minutes."""
        code = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._links = {c: t for c, t in self._links.items() if t > now}
            self._links[code] = now + LINK_SECONDS
        return code

    def redeem(self, code: str) -> bool:
        with self._lock:
            expires = self._links.pop(code, None)
        return expires is not None and expires > time.monotonic()
