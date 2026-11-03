"""M12.2: the Google Drive connector, against recorded Drive API responses (no network)."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import eridi.core.service as service_module
from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.connectors.base import ConnectorError
from eridi.connectors.gauth import GoogleAccount, GoogleSignInNeededError
from eridi.connectors.gdrive import (
    DriveClient,
    GDriveConnector,
    list_drives,
    root_id_for,
    stub_doc_id,
    whoami,
)
from eridi.connectors.tree import MemoryDeltaCache
from eridi.core.config import Settings
from eridi.core.extract.base import extract
from eridi.core.index.db import open_index
from eridi.core.index.search import search
from eridi.core.models import Root
from eridi.core.scan import scan_root
from eridi.core.service import Eridi
from eridi.core.snapshots import SnapshotStore
from gdrive_fake import MY_DRIVE, TOKEN, FakeDrive

SOP = "Protocols 2026/Cell culture SOP.gdoc"
SHEET = "Antibody lots.gsheet"
MEETING = "lab_meeting_2026-10-14.md"
ROOT = root_id_for(MY_DRIVE)


@pytest.fixture
def drive() -> FakeDrive:
    fake = FakeDrive()
    fake.content["DOC1@text/plain"] = (
        b"\xef\xbb\xbfCell culture SOP\r\nPassage at 80% confluence.\r\n"
    )
    fake.content["SHEET1@text/csv"] = b"Antibody,Lot\r\nAnti-pERK,B2291\r\n"
    fake.content["FILE1"] = b"# Lab meeting\n\n- Switch blocking buffer from milk to BSA.\n"
    fake.content["SLASH1"] = b"lane 1: 0.8\n"
    return fake


def connector(drive: FakeDrive, cache: MemoryDeltaCache | None = None) -> GDriveConnector:
    return GDriveConnector(ROOT, f"my-drive:{MY_DRIVE}", drive.client(), cache=cache)


def test_drives_and_who_is_signed_in(drive: FakeDrive) -> None:
    client = drive.client()
    assert whoami(client) == ("01234567890123456789", "a.okafor@lab.example")
    drives = list_drives(client)
    assert [(d.label, d.kind, d.location) for d in drives] == [
        ("My Drive", "my_drive", "my-drive:ROOT0"),
        ("Smith Lab Shared", "shared_drive", "shared-drive:0ASmithLabShared"),
    ]


def test_listing_names_native_documents_like_drive_for_desktop(drive: FakeDrive) -> None:
    files = {f.path: f for f in connector(drive).list_files()}
    assert sorted(files) == sorted(
        [
            SHEET,
            MEETING,
            SOP,
            "Protocols 2026/WB 1_2 results.txt",  # "/" in a Drive name can't be a folder
            "Sign-up.gform",
        ]
    )  # no shortcut, nothing from someone else's folder, no folders
    assert files[MEETING].size == 64
    assert files[SOP].modified == datetime(2026, 10, 20, 10, tzinfo=UTC).timestamp()
    listing = [p for p in drive.paths() if p.startswith("/files?")]
    assert len(listing) == 2 and "pageToken=p2" in listing[1]  # paged


def test_native_documents_are_exported_as_text(drive: FakeDrive) -> None:
    c = connector(drive)
    list(c.list_files())
    sop = c.open_bytes(SOP)
    assert extract(SOP, sop).text == "Cell culture SOP\nPassage at 80% confluence."
    assert extract(SOP, sop).status == "ok"
    sheet = extract(SHEET, c.open_bytes(SHEET))
    assert sheet.text == "Antibody,Lot\nAnti-pERK,B2291"
    assert any("mimeType=text%2Fcsv" in p for p in drive.paths())
    form = extract("Sign-up.gform", c.open_bytes("Sign-up.gform"))
    assert form.status == "cloud_document"  # a form has nothing to search but its name
    assert c.open_bytes(MEETING).startswith(b"# Lab meeting")
    assert "alt=media" in drive.paths()[-1]


def test_revisions_mapping(drive: FakeDrive) -> None:
    c = connector(drive)
    list(c.list_files())
    versions = c.versions(SOP)
    assert [(v.id, v.author) for v in versions] == [("R2", "A. Okafor"), ("R1", "B. Lee")]
    assert versions[0].modified == datetime(2026, 10, 10, 10, tzinfo=UTC).timestamp()
    drive.content["DOC1@R1"] = b"Passage at 70% confluence."
    assert c.open_version(SOP, "R1") == b"Passage at 70% confluence."  # via export link
    drive.content["FILE1@R1"] = b"old meeting notes"
    assert c.open_version(MEETING, "R1") == b"old meeting notes"


def test_changes_api_updates_the_tree(drive: FakeDrive) -> None:
    cache = MemoryDeltaCache()
    list(connector(drive, cache).list_files())
    assert json.loads(cache.data[ROOT][0] or "") == {"changes": "100"}
    drive.changes["100"] = "changes_edits"
    changes = connector(drive, cache).sync()
    assert sorted((ch.kind, ch.path) for ch in changes) == sorted(
        [
            ("added", "Protocols/Cell culture SOP.gdoc"),  # folder renamed; doc edited too
            ("added", "Protocols/WB 1_2 results.txt"),
            ("added", "Protocols/gel_2026-11-02.txt"),
            ("removed", SHEET),  # trashed
            ("removed", MEETING),  # deleted
            ("removed", SOP),
            ("removed", "Protocols 2026/WB 1_2 results.txt"),
        ]
    )
    assert json.loads(cache.data[ROOT][0] or "") == {"changes": "101"}
    assert sum(1 for p in drive.paths() if p.startswith("/files?")) == 2  # not listed again


def test_shared_drive_asks_for_that_drive_only(drive: FakeDrive) -> None:
    c = GDriveConnector("gd:0ASmithLabShared", "shared-drive:0ASmithLabShared", drive.client())
    list(c.list_files())
    listing = next(r for r in drive.requests if r.url.path.endswith("/files"))
    params = dict(listing.url.params)
    assert params["corpora"] == "drive" and params["driveId"] == "0ASmithLabShared"
    assert params["supportsAllDrives"] == "true"
    start = next(r for r in drive.requests if r.url.path.endswith("/startPageToken"))
    assert dict(start.url.params)["driveId"] == "0ASmithLabShared"


def test_scanning_a_google_drive(drive: FakeDrive, tmp_path: Path) -> None:
    conn = open_index(tmp_path / "index.sqlite")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    root = Root(ROOT, "My Drive", f"my-drive:{MY_DRIVE}", kind="gdrive")
    cache = MemoryDeltaCache()
    settings = Settings(scan_workers=1)
    first = scan_root(conn, root, connector(drive, cache), snapshots, settings)
    assert SOP in first.new and SHEET in first.new
    assert [h.path for h in search(conn, "confluence")] == [SOP]
    assert [h.path for h in search(conn, "pERK")] == [SHEET]

    exports = sum(1 for p in drive.paths() if "/export" in p)
    again = scan_root(conn, root, connector(drive, cache), snapshots, settings)
    assert not again.changed and not again.new
    assert sum(1 for p in drive.paths() if "/export" in p) == exports  # nothing re-read


def test_stub_doc_id() -> None:
    assert stub_doc_id(b'{"doc_id": "DOC1", "email": "x"}') == "DOC1"
    assert stub_doc_id(b'{"url": "https://docs.google.com/document/d/DOC2/edit"}') == "DOC2"
    assert stub_doc_id(b'{"resource_id": "document:DOC3"}') == "DOC3"
    assert stub_doc_id(b"Cell culture SOP") is None


# --- through the service ---


class FakeGoogleAuth:
    def __init__(self) -> None:
        self.signed_in = False

    def sign_in(self, identify: Callable[[str], GoogleAccount], **kwargs: Any) -> GoogleAccount:
        self.signed_in = True
        return identify(TOKEN)

    def accounts(self) -> list[GoogleAccount]:
        return (
            [GoogleAccount("01234567890123456789", "a.okafor@lab.example")]
            if self.signed_in
            else []
        )

    def token(self, account_id: str) -> str:
        if not self.signed_in:
            raise GoogleSignInNeededError()
        return TOKEN


@pytest.fixture
def lab(drive: FakeDrive, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Eridi]:
    auth = FakeGoogleAuth()
    monkeypatch.setattr(service_module, "GoogleAuth", lambda *a, **k: auth)

    def client(token: Callable[[], str]) -> DriveClient:
        http = httpx.Client(transport=httpx.MockTransport(drive.handle), follow_redirects=True)
        return DriveClient(token, http=http, sleep=lambda s: None)

    monkeypatch.setattr(service_module, "DriveClient", client)
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        yield eridi


def test_add_scan_search_and_note(lab: Eridi) -> None:
    account, drives = lab.google_sign_in()
    assert account.email == "a.okafor@lab.example"
    root = lab.add_google_root(account.id, drives[0])
    assert (root.id, root.kind, root.location) == (ROOT, "gdrive", "my-drive:ROOT0")
    lab.scan()
    [hit] = lab.search("confluence")
    assert (hit.root_label, hit.path) == ("My Drive", SOP)
    note = lab.add_note("Lowered confluence after contamination.", file=f"{ROOT}:{SOP}")
    assert note.subjects[0].root_id == ROOT
    doctor = {c.name: c for c in lab.doctor().checks}
    assert doctor["Storage 'My Drive'"].status == "ok"


def test_local_gdoc_shortcut_resolves_to_the_real_document(
    lab: Eridi, sample_lab: SampleLab
) -> None:
    sample_lab.gdoc_stub.write_text(json.dumps({"doc_id": "DOC1"}), encoding="utf-8")
    lab.add_root(sample_lab.root, "Synced folder")
    account, drives = lab.google_sign_in()
    lab.add_google_root(account.id, drives[0])
    lab.scan()
    local = "Protocols/Cell culture SOP.gdoc"
    real = lab.resolve_file(local)
    assert (real.root_id, real.path) == (ROOT, SOP)
    note = lab.add_note("Updated the passage ratio.", file=local)
    assert note.subjects[0].root_id == ROOT  # the note is about the real document
    history = lab.file_history(local)
    assert history.file.root_id == ROOT


def test_signed_out_drive_is_unavailable_not_missing(lab: Eridi, drive: FakeDrive) -> None:
    account, drives = lab.google_sign_in()
    lab.add_google_root(account.id, drives[0])
    lab.scan()
    lab.google.signed_in = False  # type: ignore[attr-defined]
    summary = lab.scan()
    assert summary.reports[0].unavailable and not summary.reports[0].missing
    assert lab.search("confluence")


def test_connector_error_when_file_is_gone(drive: FakeDrive) -> None:
    c = connector(drive)
    list(c.list_files())
    with pytest.raises(ConnectorError, match="isn't in this Google drive"):
        c.open_bytes("nope.txt")


def test_cli_add_google(lab: Eridi) -> None:
    result = CliRunner().invoke(cli, ["storage", "add-google", "--drive", "1"])
    assert result.exit_code == 0, result.output
    assert (
        "Signed in as a.okafor@lab.example" in result.output and "Added My Drive" in result.output
    )
    listing = CliRunner().invoke(cli, ["storage", "list"]).output
    assert "Google Drive" in listing
