"""A fake Microsoft Graph for tests: recorded responses served through httpx.MockTransport.

Nothing here touches the network. Routes answer with the JSON in `tests/fixtures/msgraph/`;
a test can queue extra responses (throttling, errors) for any path.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from eridi.connectors.msgraph import GRAPH, GraphClient

FIXTURES = Path(__file__).parent / "fixtures" / "msgraph"
DRIVE = "b!SmithLabDocs01"
TOKEN = "fake-access-token"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    return data


class FakeGraph:
    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.queued: dict[str, list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []
        self.content: dict[str, bytes] = {}
        """Item ID → bytes for downloads."""
        self.json(f"/drives/{DRIVE}/root/delta", "delta_page1")
        self.json(f"/drives/{DRIVE}/root/delta?token=page2", "delta_page2")
        self.json(f"/drives/{DRIVE}/root/delta?token=t1", "delta_empty")
        self.json(f"/drives/{DRIVE}/root/delta?token=t2", "delta_empty")
        self.json("/me/drive", "me_drive")
        self.json("/me/followedSites", "followed_sites")
        sites = fixture("followed_sites")["value"]
        self.json(f"/sites/{sites[0]['id']}/drives", "site_drives_smithlab")
        self.json(f"/sites/{sites[1]['id']}/drives", "site_drives_forbidden", status=403)
        self.json(f"/drives/{DRIVE}/items/FILE1/versions", "versions")

    def json(self, path: str, name: str, status: int = 200) -> None:
        body = fixture(name)
        self.routes[path] = lambda request: httpx.Response(status, json=body)

    def queue(self, path: str, response: httpx.Response) -> None:
        """Answer the next request for `path` with `response` (before the normal route)."""
        self.queued.setdefault(path, []).append(response)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "download.example":
            return httpx.Response(200, content=self.content[url.path.strip("/")])
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = str(url).removeprefix(GRAPH)
        if self.queued.get(path):
            return self.queued[path].pop(0)
        parts = url.path.split("/")
        if parts[-1] == "content" and "items" in parts:
            item_id = parts[parts.index("items") + 1]
            if "versions" in parts:
                version = parts[parts.index("versions") + 1]
                return httpx.Response(200, content=self.content[f"{item_id}@{version}"])
            # Graph answers downloads with a redirect to a pre-authenticated URL.
            return httpx.Response(302, headers={"Location": f"https://download.example/{item_id}"})
        route = self.routes.get(path)
        if route is None:
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": path}})
        return route(request)

    def client(self) -> GraphClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)
        return GraphClient(lambda: TOKEN, http=http)

    def paths(self) -> list[str]:
        return [str(r.url).removeprefix(GRAPH) for r in self.requests]
