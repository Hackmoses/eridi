"""Plain HTTPS to a cloud provider's REST API, with a bearer token and polite retries.

Shared by the Microsoft Graph and Google Drive connectors. Throttling (429), busy servers
(5xx) and dropped connections are retried, waiting as long as the provider's `Retry-After`
asks, else 1, 2, 4… s, never more than `MAX_WAIT` for one retry.
"""

import logging
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from eridi.connectors.base import ConnectorError

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(30.0, read=120.0)
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6
MAX_WAIT = 300.0
"""Never wait longer than this for one retry, whatever `Retry-After` says."""

TokenProvider = Callable[[], str]


class ApiError(ConnectorError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ApiClient:
    """Subclasses set `base` and `provider`, and turn error responses into messages."""

    base = ""
    provider = "the storage provider"

    def __init__(
        self,
        token: TokenProvider,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._token = token
        self.http = http or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        self.sleep = sleep
        self.max_attempts = max_attempts

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError(f"A client for {self.provider} can't be sent to another process.")

    def _url(self, path_or_url: str) -> str:
        return path_or_url if path_or_url.startswith("https://") else self.base + path_or_url

    def error_for(self, response: httpx.Response) -> ApiError:
        return ApiError(f"{self.provider} error {response.status_code}", response.status_code)

    def retryable(self, response: httpx.Response) -> bool:
        return response.status_code in RETRY_STATUSES

    def unreachable(self, exc: Exception) -> ApiError:
        return ApiError(
            f"Couldn't reach {self.provider} ({type(exc).__name__}). Check the internet "
            "connection; Eridi will try again at the next scan."
        )

    def request(self, path_or_url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with the token, retried as described above."""
        return self.send("GET", path_or_url, params=params)

    def send(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        """Any request, retried like `request`. `auth=False` sends no token (pre-authorized
        upload URLs must not receive it)."""
        url = self._url(path_or_url)
        for attempt in range(1, self.max_attempts + 1):
            last = attempt == self.max_attempts
            sent = dict(headers or {})
            if auth:
                sent["Authorization"] = f"Bearer {self._token()}"
            try:
                response = self.http.request(
                    method, url, params=params, content=content, json=json, headers=sent
                )
            except httpx.TransportError as exc:
                if last:
                    raise self.unreachable(exc) from None
                self.sleep(backoff(attempt))
                continue
            if self.retryable(response) and not last:
                wait = retry_after(response) or backoff(attempt)
                log.info("%s asked to wait %.0f s (HTTP %s).", self.provider, wait,
                         response.status_code)  # fmt: skip
                self.sleep(min(wait, MAX_WAIT))
                continue
            if response.status_code >= 400:
                raise self.error_for(response)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, path_or_url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        data = self.request(path_or_url, params).json()
        if not isinstance(data, dict):
            raise ApiError(f"{self.provider} returned something unexpected. Try again later.")
        return data

    def get_bytes(self, path_or_url: str, params: dict[str, str] | None = None) -> bytes:
        return self.request(path_or_url, params).content


def backoff(attempt: int) -> float:
    return float(min(2 ** (attempt - 1), 60))


def retry_after(response: httpx.Response) -> float | None:
    """Seconds from a `Retry-After` header (a number, or an HTTP date)."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())
