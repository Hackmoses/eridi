"""M10.4: adding Microsoft storage from the service, the terminal and the web app.

A fake sign-in and the recorded Graph responses stand in for Microsoft: no network.
"""

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import eridi.core.service as service_module
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.connectors.msauth import ApprovalRequiredError, MicrosoftAccount, SignInNeededError
from eridi.connectors.msgraph import GraphClient
from eridi.core.service import Eridi, EridiError
from msgraph_fake import DRIVE, TOKEN, FakeGraph
from test_api import AUTH, BASE, PORT
from test_api import TOKEN as API_TOKEN

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
ROOT = f"ms:{DRIVE}"
ACCOUNT = MicrosoftAccount("uid.tid", "a.okafor@uni.example", "tid")
CONSENT = "https://login.microsoftonline.com/tid/adminconsent?client_id=app"


class FakeAuth:
    def __init__(self) -> None:
        self.signed_in = False
        self.approval_needed = False
        self.shown: list[str] = []

    def sign_in(self, *, device_code: bool = False, show: Callable[[str], None] = print) -> Any:
        if self.approval_needed:
            raise ApprovalRequiredError(CONSENT)
        if device_code:
            show("To sign in, go to https://microsoft.com/devicelogin and enter ABCD1234.")
        self.signed_in = True
        return ACCOUNT

    def accounts(self) -> list[MicrosoftAccount]:
        return [ACCOUNT] if self.signed_in else []

    def token(self, account_id: str) -> str:
        if not self.signed_in or account_id != ACCOUNT.id:
            raise SignInNeededError()
        return TOKEN


def docx_bytes(incubation: str) -> bytes:
    doc = Document()
    doc.add_paragraph(f"Primary antibody incubation: {incubation}.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.fixture
def graph() -> FakeGraph:
    fake = FakeGraph()
    fake.content["FILE1"] = docx_bytes("1 h at RT")
    fake.content["FILE2"] = b"# Lab meeting\n\n- Switch blocking buffer from milk to BSA.\n"
    return fake


@pytest.fixture
def auth(graph: FakeGraph, monkeypatch: pytest.MonkeyPatch) -> FakeAuth:
    """Every Eridi made in this test (including the CLI's) signs in with the fakes."""
    fake = FakeAuth()
    monkeypatch.setattr(service_module, "MicrosoftAuth", lambda *a, **k: fake)

    def client(token: Callable[[], str]) -> GraphClient:
        http = httpx.Client(transport=httpx.MockTransport(graph.handle), follow_redirects=True)
        return GraphClient(token, http=http, sleep=lambda s: None)

    monkeypatch.setattr(service_module, "GraphClient", client)
    return fake


@pytest.fixture
def lab(auth: FakeAuth, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        yield eridi


def test_sign_in_add_scan_and_search(lab: Eridi, graph: FakeGraph) -> None:
    account, drives = lab.microsoft_sign_in()
    assert account == ACCOUNT
    assert [d.label for d in drives] == ["OneDrive", "Smith Lab", "Smith Lab – Protocols"]
    root = lab.add_microsoft_root(account.id, drives[1])
    assert (root.id, root.kind, root.label, root.account) == (
        ROOT,
        "msgraph",
        "Smith Lab",
        ACCOUNT.id,
    )
    with pytest.raises(EridiError, match="already connected"):
        lab.add_microsoft_root(account.id, DRIVE)

    summary = lab.scan()
    assert summary.total("new") == 2
    hit = lab.search("blocking buffer")[0]
    assert (hit.root_label, hit.path) == ("Smith Lab", "lab_meeting_2026-10-14.md")
    # File IDs contain the storage ID's own ":" and still resolve.
    info = lab.resolve_file(f"{ROOT}:{PROTOCOL}")
    assert info.root_id == ROOT and info.path == PROTOCOL
    note = lab.add_note("Checked with the supplier.", file=f"{ROOT}:{PROTOCOL}")
    assert note.subjects[0].root_id == ROOT

    graph.json(f"/drives/{DRIVE}/root/delta?token=t1", "delta_changes")
    graph.json(f"/drives/{DRIVE}/root/delta?token=t2", "delta_empty")
    graph.content["FILE1"] = docx_bytes("2 h at RT")
    graph.content["FILE3"] = b"Antibody,Lot\nAnti-pERK,B2291\n"
    lab.scan()
    assert {f.path for f in lab.find_files("")} >= {"Antibody_lot_log_2026.csv"}

    project = lab.create_project("Blots", folders=[f"{ROOT}:Protocols 2026"])
    assert project.folders[0].root_id == ROOT


def test_doctor_and_removing_microsoft_storage(lab: Eridi, auth: FakeAuth) -> None:
    account, drives = lab.microsoft_sign_in()
    root = lab.add_microsoft_root(account.id, drives[1])
    lab.scan()
    [check] = [c for c in lab.doctor().checks if c.name == "Storage 'Smith Lab'"]
    assert check.status == "ok" and "a.okafor@uni.example" in check.detail
    auth.signed_in = False
    [check] = [c for c in lab.doctor().checks if c.name == "Storage 'Smith Lab'"]
    assert check.status == "warning" and "add-microsoft" in check.detail
    report = lab.scan()
    assert "no longer signed in" in (report.reports[0].unavailable or "")
    assert lab.find_files("lab_meeting")  # nothing marked missing while signed out

    lab.remove_root(root.id)
    assert lab.conn.execute("SELECT count(*) FROM cloud_items").fetchone()[0] == 0


def test_add_microsoft_command(lab: Eridi, auth: FakeAuth) -> None:
    runner = CliRunner()
    out = runner.invoke(cli, ["storage", "add-microsoft"]).output
    assert "Signed in as a.okafor@uni.example" in out
    assert "2. Smith Lab" in out and "Choose with --drive" in out
    result = runner.invoke(cli, ["storage", "add-microsoft", "--device-code", "--drive", "2"])
    assert result.exit_code == 0, result.output
    assert "devicelogin" in result.output and "Added Smith Lab" in result.output
    assert "(connected)" in runner.invoke(cli, ["storage", "add-microsoft"]).output
    listed = runner.invoke(cli, ["storage", "list"]).output
    assert "Smith Lab  Microsoft" in listed
    bad = runner.invoke(cli, ["storage", "add-microsoft", "--drive", "Nope"])
    assert bad.exit_code == 1 and "No single library" in bad.output

    auth.approval_needed = True
    blocked = runner.invoke(cli, ["storage", "add-microsoft"])
    assert blocked.exit_code == 1
    assert "Your university needs to approve this app." in blocked.output
    assert CONSENT in blocked.output and "IT_APPROVAL.md" in blocked.output


@pytest.fixture
def client(lab: Eridi) -> Iterator[TestClient]:
    app = create_app(lab, token=API_TOKEN, port=PORT, scanner=BackgroundScanner(lab, 0))
    with TestClient(app, base_url=BASE, headers=AUTH) as c:
        yield c


def test_add_microsoft_storage_in_the_web_app(client: TestClient, lab: Eridi) -> None:
    assert client.get("/api/microsoft/sign-in").json()["state"] == "idle"
    refused = client.post("/api/microsoft/storage", json={"drive_id": DRIVE})
    assert refused.status_code == 400 and "Sign in" in refused.json()["error"]

    started = client.post("/api/microsoft/sign-in", json={"device_code": True})
    assert started.status_code == 202
    client.app.state.microsoft.wait()  # type: ignore[attr-defined]
    status = client.get("/api/microsoft/sign-in").json()
    assert status["state"] == "done" and status["username"] == "a.okafor@uni.example"
    assert [d["label"] for d in status["drives"]][1] == "Smith Lab"
    assert not any(d["connected"] for d in status["drives"])

    added = client.post("/api/microsoft/storage", json={"drive_id": DRIVE})
    assert added.status_code == 201 and added.json()["id"] == ROOT
    assert client.get("/api/microsoft/sign-in").json()["drives"][1]["connected"] is True
    storage = client.get("/api/storage").json()["storage"]
    assert [(s["label"], s["kind"]) for s in storage] == [("Smith Lab", "msgraph")]
    again = client.post("/api/microsoft/storage", json={"drive_id": DRIVE})
    assert again.status_code == 400 and "already connected" in again.json()["error"]


def test_web_app_explains_it_approval(client: TestClient, auth: FakeAuth) -> None:
    auth.approval_needed = True
    client.post("/api/microsoft/sign-in", json={})
    client.app.state.microsoft.wait()  # type: ignore[attr-defined]
    status = client.get("/api/microsoft/sign-in").json()
    assert status["state"] == "approval"
    assert status["consent_url"] == CONSENT
    assert status["message"].startswith("Your university needs to approve this app.")


def test_without_an_app_id_the_terminal_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No traceback: the message names the setting to fill in."""
    from eridi.connectors import msauth

    monkeypatch.delenv(msauth.ENV_CLIENT_ID, raising=False)
    monkeypatch.setattr(msauth, "DEFAULT_CLIENT_ID", "")
    runner = CliRunner()
    runner.invoke(cli, ["init", "--name", "A", "--email", "", "--record-folder", str(tmp_path)])
    result = runner.invoke(cli, ["storage", "add-microsoft"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output and "no Microsoft app ID" in result.output
    assert "docs/MICROSOFT.md" in result.output
