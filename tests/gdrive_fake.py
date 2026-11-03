"""A fake Google Drive API for tests: recorded responses through httpx.MockTransport.

Nothing here touches the network. Routes answer with the JSON in `tests/fixtures/gdrive/`;
a test can queue extra responses (throttling, errors) for any route key.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from eridi.connectors.gdrive import DRIVE_API, DriveClient

FIXTURES = Path(__file__).parent / "fixtures" / "gdrive"
TOKEN = "fake-google-token"  # noqa: S105 - test value
MY_DRIVE = "ROOT0"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    return data


def not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})


class FakeDrive:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.queued: dict[str, list[httpx.Response]] = {}
        self.changes: dict[str, str] = {"100": "changes_empty", "101": "changes_empty"}
        """Page token → fixture name for `/changes`."""
        self.content: dict[str, bytes] = {}
        """File ID (or `ID@mime` for exports, `ID@rev` for revisions) → bytes."""

    def queue(self, key: str, response: httpx.Response) -> None:
        """Answer the next request for route `key` (e.g. "/files", "/changes") with this."""
        self.queued.setdefault(key, []).append(response)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "docs.google.com":  # a revision's export link
            return httpx.Response(200, content=self.content[url.path.strip("/")])
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = url.path.removeprefix("/drive/v3")
        params = dict(url.params)
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/about":
            return httpx.Response(200, json=fixture("about"))
        if path == "/files/root":
            return httpx.Response(200, json=fixture("files_root"))
        if path == "/drives":
            return httpx.Response(200, json=fixture("drives"))
        if path == "/changes/startPageToken":
            return httpx.Response(200, json=fixture("start_token"))
        if path == "/files":
            page = "files_page2" if params.get("pageToken") == "p2" else "files_page1"
            return httpx.Response(200, json=fixture(page))
        if path == "/changes":
            name = self.changes.get(params.get("pageToken", ""))
            if name is None:
                return httpx.Response(400, json=fixture("error_invalid_token"))
            return httpx.Response(200, json=fixture(name))
        parts = path.strip("/").split("/")  # files/<id>[/export | /revisions[/<rid>]]
        if parts[0] == "files" and len(parts) >= 2:
            file_id = parts[1]
            if len(parts) == 3 and parts[2] == "export":
                return self._bytes(f"{file_id}@{params['mimeType']}")
            if len(parts) == 3 and parts[2] == "revisions":
                return httpx.Response(200, json=fixture("revisions"))
            if len(parts) == 4 and parts[2] == "revisions":
                if params.get("fields") == "exportLinks":
                    link = f"https://docs.google.com/{file_id}@{parts[3]}"
                    return httpx.Response(200, json={"exportLinks": {"text/plain": link}})
                return self._bytes(f"{file_id}@{parts[3]}")
            if len(parts) == 2 and params.get("alt") == "media":
                return self._bytes(file_id)
        return not_found()

    def _bytes(self, key: str) -> httpx.Response:
        if key not in self.content:
            return httpx.Response(
                404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}}
            )
        return httpx.Response(200, content=self.content[key])

    def client(self, sleep: Callable[[float], None] = lambda s: None) -> DriveClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)
        return DriveClient(lambda: TOKEN, http=http, sleep=sleep)

    def paths(self) -> list[str]:
        return [str(r.url).removeprefix(DRIVE_API) for r in self.requests]
