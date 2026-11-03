"""M7.1–7.2: local API security and endpoints."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from conftest import SampleLab
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.api.security import COOKIE_NAME, Guard, get_or_create_token, stored_token
from eridi.core.service import Eridi

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "test-token-0123456789"  # noqa: S105 - test value
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def eridi(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as e:
        e.init("A. Okafor", "a@example.edu", tmp_path / "record")
        e.add_root(sample_lab.root, "Lab Drive")
        yield e


@pytest.fixture
def client(eridi: Eridi) -> Iterator[TestClient]:
    app = create_app(eridi, token=TOKEN, port=PORT, scanner=BackgroundScanner(eridi, 0))
    with TestClient(app, base_url=BASE) as c:
        yield c


def edit_protocol(sample_lab: SampleLab, text: str) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = text
    doc.save(str(sample_lab.protocol_docx))


# --- security ---


def test_request_without_token_is_401(client: TestClient) -> None:
    for path in ("/api/status", "/api/search?q=x", "/", "/ui/app.js"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert "eridi open" in response.text
    assert client.post("/api/notes", json={"text": "x"}).status_code == 401


def test_wrong_token_is_401(client: TestClient) -> None:
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    client.cookies.set(COOKIE_NAME, "forged")
    assert client.get("/api/status").status_code == 401


def test_wrong_host_is_400(client: TestClient) -> None:
    for host in ("evil.example", "127.0.0.1", f"127.0.0.1:{PORT + 1}", f"attacker.test:{PORT}"):
        response = client.get("/api/status", headers={**AUTH, "Host": host})
        assert response.status_code == 400, host
    assert (
        client.get("/api/status", headers={**AUTH, "Host": f"localhost:{PORT}"}).status_code == 200
    )


def test_cross_origin_post_is_403(client: TestClient) -> None:
    for origin in ("http://evil.example", f"http://127.0.0.1:{PORT + 1}", "null"):
        response = client.post(
            "/api/notes", json={"text": "planted"}, headers={**AUTH, "Origin": origin}
        )
        assert response.status_code == 403, origin
    response = client.post(
        "/api/notes", json={"text": "x"}, headers={**AUTH, "Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    same = client.post("/api/notes", json={"text": "fine"}, headers={**AUTH, "Origin": BASE})
    assert same.status_code == 201


def test_no_cors_headers_and_security_headers(client: TestClient) -> None:
    response = client.get("/api/status", headers={**AUTH, "Origin": BASE})
    assert "access-control-allow-origin" not in response.headers
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_one_time_link_sets_session_cookie(client: TestClient) -> None:
    link = client.post("/api/session-link", headers=AUTH).json()["url"]
    assert link.startswith(f"{BASE}/?t=")
    path = link.removeprefix(BASE)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert TOKEN not in cookie  # the cookie isn't the API token itself
    assert client.get("/api/status").status_code == 200  # signed in by cookie
    assert client.get("/").status_code == 200
    client.cookies.clear()
    assert client.get(path, follow_redirects=False).status_code == 401  # used up


def test_session_link_needs_the_bearer_token(client: TestClient) -> None:
    code = client.app.state.guard.new_link()  # type: ignore[attr-defined]
    client.get(f"/?t={code}")
    assert client.post("/api/session-link").status_code == 403  # cookie alone isn't enough


def test_guard_rules() -> None:
    guard = Guard("t", 9)
    assert guard.host_ok("127.0.0.1:9") and guard.host_ok("localhost:9")
    assert not guard.host_ok(None)
    assert guard.origin_ok(None, None) and guard.origin_ok("http://localhost:9", "same-origin")
    assert not guard.origin_ok("http://localhost:9", "cross-site")
    assert not guard.redeem("never-issued")


def test_token_lives_in_the_keychain(memory_keyring: dict[tuple[str, str], str]) -> None:
    assert stored_token() is None
    token = get_or_create_token()
    assert len(token) >= 40
    assert get_or_create_token() == token
    assert memory_keyring[("Eridi", "local-api-token")] == token
    assert stored_token() == token


def test_no_keychain_gives_a_token_for_this_run_only(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    import keyring
    from keyring.errors import NoKeyringError

    def broken(*args: object) -> None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setattr(keyring, "set_password", broken)
    first, second = get_or_create_token(), get_or_create_token()
    assert first != second
    assert not any(
        first in p.read_text(errors="ignore") for p in isolated_home.rglob("*") if p.is_file()
    )


# --- endpoints ---


def test_full_flow(client: TestClient, sample_lab: SampleLab) -> None:
    scan = client.post("/api/scan?wait=true", headers=AUTH).json()
    assert scan["finished"] and scan["summary"]["storage"][0]["new"] == 9
    assert client.get("/api/changes", headers=AUTH).json() == {"changes": []}

    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    client.post("/api/scan?wait=true", headers=AUTH)
    [change] = client.get("/api/changes", headers=AUTH).json()["changes"]
    assert change["path"] == PROTOCOL
    assert change["summary"] == "Primary antibody incubation: 1 h at RT → 2 h at RT"
    assert client.get("/api/status", headers=AUTH).json()["pending_count"] == 1

    diff = client.get("/api/diff", params={"file": change["file_id"]}, headers=AUTH).json()
    assert {"op": "+", "text": "Primary antibody incubation: 2 h at RT."} in diff["lines"]

    note = client.post(
        "/api/notes",
        json={"text": "New lot needs longer.", "file": change["file_id"], "outcome": "worked"},
        headers=AUTH,
    )
    assert note.status_code == 201
    saved = note.json()
    assert saved["change"] == change["summary"] and saved["outcome"] == "worked"
    assert client.get("/api/changes", headers=AUTH).json() == {"changes": []}

    history = client.get("/api/history", params={"file": change["file_id"]}, headers=AUTH).json()
    assert len(history["versions"]) == 2
    assert [e["id"] for e in history["entries"]] == [saved["id"]]
    assert history["file"]["storage"] == "Lab Drive"

    files = client.get("/api/search", params={"q": "primary"}, headers=AUTH).json()["results"]
    protocol = next(r for r in files if r["path"] == PROTOCOL)
    assert protocol["history"] == {
        "versions": 2, "notes": 1, "last_edited": protocol["history"]["last_edited"],
        "needs_why": False,
    }  # fmt: skip
    results = client.get("/api/search", params={"q": "longer"}, headers=AUTH).json()["results"]
    assert results[0]["id"] == saved["id"]
    assert ["longer", True] in results[0]["snippet"]

    outcome = client.post("/api/outcomes", json={"outcome": "partial", "entry_id": saved["id"]},
                          headers=AUTH)  # fmt: skip
    assert outcome.status_code == 201
    recent = client.get("/api/recent", headers=AUTH).json()["entries"]
    assert [e["kind"] for e in recent] == ["outcome", "note"]


def test_dismiss(client: TestClient, sample_lab: SampleLab) -> None:
    client.post("/api/scan?wait=true", headers=AUTH)
    edit_protocol(sample_lab, "Primary antibody incubation: 5 h at RT.")
    client.post("/api/scan?wait=true", headers=AUTH)
    [change] = client.get("/api/changes", headers=AUTH).json()["changes"]
    assert (
        client.post("/api/changes/dismiss", json={"key": change["key"]}, headers=AUTH).status_code
        == 200
    )
    assert client.get("/api/changes", headers=AUTH).json() == {"changes": []}


def test_storage_list_and_toggles(client: TestClient) -> None:
    [root] = client.get("/api/storage", headers=AUTH).json()["storage"]
    assert root["search"] and not root["save"]
    updated = client.patch(f"/api/storage/{root['id']}", json={"search": False, "save": True},
                           headers=AUTH).json()  # fmt: skip
    assert not updated["search"] and updated["save"]
    response = client.patch("/api/storage/root_nope", json={"search": True}, headers=AUTH)
    assert response.status_code == 400 and "storage list" in response.json()["error"]


def test_files_lookup_and_errors(client: TestClient) -> None:
    client.post("/api/scan?wait=true", headers=AUTH)
    found = client.get("/api/files", params={"q": "papers"}, headers=AUTH).json()["files"]
    assert len(found) == 2
    response = client.get("/api/history", params={"file": "Papers/"}, headers=AUTH)
    assert response.status_code == 409 and len(response.json()["candidates"]) == 2
    response = client.get("/api/history", params={"file": "nothing like it"}, headers=AUTH)
    assert response.status_code == 404
    response = client.post("/api/notes", json={"text": "x", "outcome": "great"}, headers=AUTH)
    assert response.status_code == 400 and "Outcome" in response.json()["error"]
    assert client.post("/api/notes", json={"text": ""}, headers=AUTH).status_code == 422


def test_background_scan_on_request(client: TestClient) -> None:
    import time

    response = client.post("/api/scan", headers=AUTH)
    assert response.status_code == 202 and response.json()["started"]
    for _ in range(200):
        scan = client.get("/api/status", headers=AUTH).json()["scan"]
        if scan["last_finished"] and not scan["running"]:
            break
        time.sleep(0.05)
    assert scan["last_summary"]["storage"][0]["new"] == 9


def test_scan_without_storage(tmp_path: Path) -> None:
    with Eridi() as eridi:
        app = create_app(eridi, token=TOKEN, port=PORT, scanner=BackgroundScanner(eridi, 0))
        with TestClient(app, base_url=BASE) as c:
            response = c.post("/api/scan", headers=AUTH)
            assert response.status_code == 400 and "storage add" in response.json()["error"]
            assert c.get("/api/status", headers=AUTH).json()["initialized"] is False


def test_eridi_open_signs_in_to_a_running_server(eridi: Eridi) -> None:
    import urllib.request

    from typer.testing import CliRunner

    from eridi.cli.main import app as cli
    from live_server import live_server

    with live_server(eridi) as (port, _):
        result = CliRunner().invoke(cli, ["open", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert f"running at http://127.0.0.1:{port}" in result.output
        link = result.output.split("Open: ")[1].strip()
        import http.cookiejar

        browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        with browser.open(link) as response:
            assert response.status == 200  # the link signed in and loaded the page


def test_stale_server_file_is_ignored(eridi: Eridi) -> None:
    from eridi.api.app import server_file
    from eridi.cli.main import running_server

    server_file().write_text('{"port": 1}', encoding="utf-8")
    assert running_server() is None  # a stale file from a crashed run is ignored
