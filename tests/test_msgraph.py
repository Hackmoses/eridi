"""M10.2: the Microsoft Graph connector, against recorded Graph responses."""

import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document

from eridi.connectors.base import ConnectorError, RemoteChange
from eridi.connectors.msgraph import (
    GraphError,
    MemoryDeltaCache,
    MsGraphConnector,
    list_drives,
    root_id_for,
)
from eridi.core.config import Settings
from eridi.core.index.db import open_index
from eridi.core.index.search import search
from eridi.core.models import Root
from eridi.core.scan import scan_root
from eridi.core.snapshots import SnapshotStore
from msgraph_fake import DRIVE, FakeGraph, fixture

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
RENAMED = "Protocols 2026/Western_blot_blocking_v7.docx"


def docx_bytes(incubation: str) -> bytes:
    doc = Document()
    doc.add_heading("Western blot: blocking and antibody incubation", level=1)
    doc.add_paragraph("Block membrane in 5% BSA in TBST for 1 h at room temperature.")
    doc.add_paragraph(f"Primary antibody incubation: {incubation}.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.fixture
def graph() -> FakeGraph:
    fake = FakeGraph()
    fake.content["FILE1"] = docx_bytes("1 h at RT")
    fake.content["FILE2"] = b"# Lab meeting\n\n- Switch blocking buffer from milk to BSA.\n"
    fake.content["FILE3"] = b"Antibody,Lot\nAnti-pERK,B2291\n"
    fake.content["FILE1@2.0"] = docx_bytes("1 h at RT")
    return fake


def connector(graph: FakeGraph, cache: MemoryDeltaCache | None = None) -> MsGraphConnector:
    return MsGraphConnector(root_id_for(DRIVE), DRIVE, graph.client(), cache=cache)


def test_root_ids() -> None:
    assert root_id_for("b!abc") == "ms:b!abc"


def test_list_drives(graph: FakeGraph) -> None:
    drives = list_drives(graph.client())
    assert [(d.id, d.kind, d.label) for d in drives] == [
        ("b!OkaforDrive01", "business", "OneDrive"),
        ("b!SmithLabDocs01", "documentLibrary", "Smith Lab"),
        ("b!SmithLabProto02", "documentLibrary", "Smith Lab – Protocols"),
    ]  # the Core Facility site refused access and is skipped


def test_listing_pages_through_delta(graph: FakeGraph) -> None:
    files = list(connector(graph).list_files())
    assert [(f.path, f.size) for f in files] == [
        (PROTOCOL, 36912),
        ("lab_meeting_2026-10-14.md", 92),
    ]  # no Office temp file, OneNote notebook or shortcut to another drive
    assert files[0].modified == datetime(2026, 10, 14, 13, 5, 22, tzinfo=UTC).timestamp()
    assert graph.paths() == [
        f"/drives/{DRIVE}/root/delta",
        f"/drives/{DRIVE}/root/delta?token=page2",
    ]


def test_downloads_follow_the_redirect_without_the_token(graph: FakeGraph) -> None:
    c = connector(graph)
    assert c.open_bytes("lab_meeting_2026-10-14.md").startswith(b"# Lab meeting")
    download = graph.requests[-1]
    assert download.url.host == "download.example"
    assert "Authorization" not in download.headers  # the token never leaves Microsoft
    with pytest.raises(ConnectorError, match="isn't in this Microsoft drive"):
        c.open_bytes("nope.txt")


def test_delta_with_changes_deletions_and_moves(graph: FakeGraph) -> None:
    cache = MemoryDeltaCache()
    c = connector(graph, cache)
    list(c.list_files())
    graph.json(f"/drives/{DRIVE}/root/delta?token=t1", "delta_changes")
    changes = connector(graph, cache).sync()  # a later scan, with the saved cursor
    assert sorted((ch.kind, ch.path) for ch in changes) == [
        ("added", "Antibody_lot_log_2026.csv"),
        ("added", RENAMED),
        ("removed", PROTOCOL),
        ("removed", "lab_meeting_2026-10-14.md"),
    ]
    assert graph.paths()[-1] == f"/drives/{DRIVE}/root/delta?token=t1"
    after = connector(graph, cache)
    assert [f.path for f in after.list_files()] == ["Antibody_lot_log_2026.csv", RENAMED]
    assert cache.data[root_id_for(DRIVE)][0].endswith("token=t2")


def test_changes_since(graph: FakeGraph) -> None:
    c = connector(graph)
    everything, cursor = c.changes_since(None)
    assert {ch.path for ch in everything} == {PROTOCOL, "lab_meeting_2026-10-14.md"}
    assert all(ch.kind == "added" for ch in everything)
    assert cursor.endswith("token=t1")
    list(c.list_files())
    graph.json(f"/drives/{DRIVE}/root/delta?token=t1", "delta_changes")
    later, cursor = c.changes_since(cursor)
    assert RemoteChange("lab_meeting_2026-10-14.md", "removed") in later
    assert cursor.endswith("token=t2")


def test_version_mapping(graph: FakeGraph) -> None:
    c = connector(graph)
    versions = c.versions(PROTOCOL)
    assert [(v.id, v.size, v.author) for v in versions] == [
        ("2.0", 36912, "A. Okafor"),
        ("1.0", 35001, None),
    ]  # the current version (3.0) is left out; newest first
    assert versions[0].modified == datetime(2026, 10, 14, 13, 5, 22, tzinfo=UTC).timestamp()
    assert c.open_version(PROTOCOL, "2.0") == graph.content["FILE1@2.0"]


def test_errors_say_what_to_do(graph: FakeGraph) -> None:
    import httpx

    graph.queue(f"/drives/{DRIVE}/root/delta", httpx.Response(401, json={"error": {}}))
    with pytest.raises(GraphError, match="Sign in again"):
        list(connector(graph).list_files())
    graph.queue(
        f"/drives/{DRIVE}/root/delta", httpx.Response(403, json=fixture("site_drives_forbidden"))
    )
    with pytest.raises(GraphError, match="IT_APPROVAL"):
        list(connector(graph).list_files())


def test_scanning_a_microsoft_drive(graph: FakeGraph, tmp_path: Path) -> None:
    """The scanner treats a drive like any storage: new, changed, moved and missing files."""
    conn = open_index(tmp_path / "index.sqlite")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    root = Root(root_id_for(DRIVE), "Smith Lab", DRIVE, kind="msgraph")
    cache = MemoryDeltaCache()
    settings = Settings(scan_workers=1)
    first = scan_root(conn, root, connector(graph, cache), snapshots, settings)
    assert first.new == [PROTOCOL, "lab_meeting_2026-10-14.md"]
    assert [h.path for h in search(conn, "blocking buffer")][:1] == ["lab_meeting_2026-10-14.md"]

    downloads = len([r for r in graph.requests if r.url.host == "download.example"])
    again = scan_root(conn, root, connector(graph, cache), snapshots, settings)
    assert again.unchanged == 2 and not again.changed
    assert len([r for r in graph.requests if r.url.host == "download.example"]) == downloads

    # The second scan's (empty) delta moved the cursor on to t2.
    graph.json(f"/drives/{DRIVE}/root/delta?token=t2", "delta_changes")
    graph.content["FILE1"] = docx_bytes("2 h at RT")
    later = scan_root(conn, root, connector(graph, cache), snapshots, settings)
    assert later.new == ["Antibody_lot_log_2026.csv", RENAMED]  # renamed and edited
    assert later.missing == [PROTOCOL, "lab_meeting_2026-10-14.md"]
