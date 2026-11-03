"""M8.2: project endpoints for the web app."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import SampleLab
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.core.service import Eridi
from test_api import AUTH, BASE, PORT, PROTOCOL, TOKEN, edit_protocol


@pytest.fixture
def client(sample_lab: SampleLab, tmp_path: Path) -> Iterator[TestClient]:
    shared = tmp_path / "Shared"
    (shared / "Imaging").mkdir(parents=True)
    (shared / "Imaging" / "confocal.md").write_text("Laser power for stains.\n", "utf-8")
    with Eridi() as e:
        e.init("A. Okafor", "a@example.edu", tmp_path / "record")
        e.add_root(sample_lab.root, "Lab Drive")
        e.add_root(shared, "Shared")
        e.scan()
        app = create_app(e, token=TOKEN, port=PORT, scanner=BackgroundScanner(e, 0))
        with TestClient(app, base_url=BASE, headers=AUTH) as c:
            yield c


def test_project_flow(client: TestClient, sample_lab: SampleLab, tmp_path: Path) -> None:
    assert client.get("/api/projects").json() == {"projects": []}
    created = client.post(
        "/api/projects",
        json={"name": "Western blots", "folders": [str(sample_lab.root / "Protocols")]},
    )
    assert created.status_code == 201, created.text
    p = created.json()
    assert [f["path"] for f in p["folders"]] == ["Protocols"]
    assert p["folders"][0]["storage"] == "Lab Drive"
    base = f"/api/projects/{p['id']}"

    added = client.post(base + "/folders", json={"folder": str(tmp_path / "Shared" / "Imaging")})
    assert added.status_code == 200, added.text
    assert {f["storage"] for f in added.json()["folders"]} == {"Lab Drive", "Shared"}

    renamed = client.patch(base, json={"name": "Western blots 2026"})
    assert renamed.json()["name"] == "Western blots 2026"
    shared_id = next(f["storage_id"] for f in added.json()["folders"] if f["storage"] == "Shared")
    homed = client.patch(base, json={"home_storage_id": shared_id})
    assert homed.json()["home_storage"] == "Shared"
    assert homed.json()["name"] == "Western blots 2026"

    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    client.post("/api/scan?wait=true")
    page = client.get(base).json()
    paths = {f["path"] for f in page["files"]}
    assert {PROTOCOL, "Imaging/confocal.md"} <= paths
    assert "Notes/lab_meeting_2026-10-14.md" not in paths
    assert [c["path"] for c in page["changes"]] == [PROTOCOL]
    assert page["entries"][0]["kind"] == "project"
    assert page["entries"][0]["project_name"] == "Western blots 2026"

    note = client.post("/api/notes", json={"text": "Project-wide plan.", "project": p["id"]})
    assert note.status_code == 201 and note.json()["project_id"] == p["id"]
    page = client.get(base).json()
    assert page["entries"][0]["text"] == "Project-wide plan."

    scoped = client.get("/api/search", params={"q": "laser", "project": p["id"]}).json()
    assert [r["path"] for r in scoped["results"]] == ["Imaging/confocal.md"]
    assert client.get("/api/changes", params={"project": p["id"]}).json()["changes"]

    removed = client.post(base + "/folders/remove", json={"folder": f"{shared_id}:Imaging"}).json()
    assert [f["storage"] for f in removed["folders"]] == ["Lab Drive"]
    assert client.get("/api/projects").json()["projects"][0]["name"] == "Western blots 2026"


def test_project_errors(client: TestClient) -> None:
    assert client.get("/api/projects/nope").status_code == 404
    client.post("/api/projects", json={"name": "Western blots"})
    dup = client.post("/api/projects", json={"name": "western blots"})
    assert dup.status_code == 400 and "already a project" in dup.json()["error"]
    bad = client.post("/api/projects/western/folders", json={"folder": "/no/such/place"})
    assert bad.status_code == 400 and "connected storage" in bad.json()["error"]
    assert client.post("/api/search?q=x&project=nope").status_code == 405
    assert client.get("/api/search", params={"q": "x", "project": "nope"}).status_code == 404
