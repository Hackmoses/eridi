"""M4.2: scanning roots for new, changed, moved and missing files."""

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document

import eridi.core.scan as scan_module
from conftest import SampleLab
from eridi.connectors.local import LocalConnector
from eridi.core.config import Settings
from eridi.core.index.db import open_index
from eridi.core.index.files import file_moves, get_file, list_files, text_versions
from eridi.core.models import Root
from eridi.core.scan import ScanReport, scan_root, scan_roots
from eridi.core.snapshots import SnapshotStore

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_index(Path(":memory:"))
    yield c
    c.close()


@pytest.fixture
def snaps() -> SnapshotStore:
    return SnapshotStore()


class Lab:
    def __init__(self, sample_lab: SampleLab, conn: sqlite3.Connection, snaps: SnapshotStore):
        self.sample = sample_lab
        self.conn = conn
        self.snaps = snaps
        self.root = Root("root_LAB", "Lab Drive", str(sample_lab.root))
        self.connector = LocalConnector(sample_lab.root, self.root.id)

    def scan(self, settings: Settings | None = None) -> ScanReport:
        return scan_root(self.conn, self.root, self.connector, self.snaps, settings)

    def snapshot_count(self) -> int:
        return len(list(self.snaps.folder.glob("??/*.txt.gz")))


@pytest.fixture
def lab(sample_lab: SampleLab, conn: sqlite3.Connection, snaps: SnapshotStore) -> Lab:
    return Lab(sample_lab, conn, snaps)


def edit_protocol(path: Path, new_line: str) -> None:
    doc = Document(str(path))
    doc.paragraphs[2].text = new_line
    doc.save(str(path))


def test_first_scan_indexes_everything(lab: Lab) -> None:
    report = lab.scan()
    assert len(report.new) == 9  # every sample file except the Office temp file
    assert PROTOCOL in report.new
    assert report.changed == [] and report.missing == [] and report.moved == []
    assert report.errors == []
    assert report.extracted == 9
    stored = get_file(lab.conn, lab.root.id, PROTOCOL)
    assert stored is not None and stored.extract_status == "ok"
    assert stored.content_sha256 and stored.text_sha256
    assert lab.snaps.get(stored.text_sha256) is not None
    assert lab.snapshot_count() == 7  # files with text; the scan and the gdoc have none
    assert len(lab.snaps.read_log()) == 7


def test_second_scan_reextracts_nothing(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    lab.scan()
    calls: list[str] = []
    real = scan_module.extract

    def spy(name: str, data: bytes) -> Any:
        calls.append(name)
        return real(name, data)

    monkeypatch.setattr(scan_module, "extract", spy)
    reads: list[str] = []
    real_open = lab.connector.open_bytes
    monkeypatch.setattr(lab.connector, "open_bytes", lambda p: reads.append(p) or real_open(p))
    report = lab.scan()
    assert calls == [] and reads == []
    assert report.unchanged == 9
    assert report.new == report.changed == []


def test_editing_the_protocol_is_one_change_and_one_snapshot(lab: Lab) -> None:
    lab.scan()
    before = lab.snapshot_count()
    edit_protocol(lab.sample.protocol_docx, "Primary antibody incubation: 2 h at RT.")
    report = lab.scan()
    assert report.changed == [PROTOCOL]
    assert report.new == [] and report.missing == []
    assert report.extracted == 1
    assert lab.snapshot_count() == before + 1
    stored = get_file(lab.conn, lab.root.id, PROTOCOL)
    assert stored is not None and stored.id is not None
    assert len(text_versions(lab.conn, stored.id)) == 2


def test_touch_without_content_change_is_no_change(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab.scan()
    path = lab.sample.notes_md
    later = path.stat().st_mtime + 100
    os.utime(path, (later, later))
    calls: list[str] = []
    real = scan_module.extract
    monkeypatch.setattr(scan_module, "extract", lambda n, d: calls.append(n) or real(n, d))
    report = lab.scan()
    assert report.changed == [] and report.new == []
    assert report.unchanged == 9
    assert calls == []  # same bytes: hashed, not extracted
    stored = get_file(lab.conn, lab.root.id, "Notes/lab_meeting_2026-10-14.md")
    assert stored is not None and stored.modified == later
    # And the scan after that doesn't even read it.
    assert lab.scan().unchanged == 9


def test_rename_is_a_move_and_keeps_history(lab: Lab) -> None:
    lab.scan()
    edit_protocol(lab.sample.protocol_docx, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    old = get_file(lab.conn, lab.root.id, PROTOCOL)
    assert old is not None and old.id is not None
    history = text_versions(lab.conn, old.id)
    new_path = lab.sample.root / "Protocols" / "Western_blot_blocking_v8.docx"
    lab.sample.protocol_docx.rename(new_path)

    report = lab.scan()
    assert report.moved == [(PROTOCOL, "Protocols/Western_blot_blocking_v8.docx")]
    assert report.new == [] and report.missing == []
    assert get_file(lab.conn, lab.root.id, PROTOCOL) is None
    moved = get_file(lab.conn, lab.root.id, "Protocols/Western_blot_blocking_v8.docx")
    assert moved is not None and moved.id is not None
    assert text_versions(lab.conn, moved.id) == history
    assert [(f, t) for f, t, _ in file_moves(lab.conn, moved.id)] == [
        (PROTOCOL, "Protocols/Western_blot_blocking_v8.docx")
    ]
    assert any(ln.kind == "move" for ln in lab.snaps.read_log())


def test_copy_is_not_a_move(lab: Lab) -> None:
    lab.scan()
    copy = lab.sample.root / "Protocols" / "copy.docx"
    copy.write_bytes(lab.sample.protocol_docx.read_bytes())
    report = lab.scan()
    assert report.new == ["Protocols/copy.docx"]
    assert report.moved == []


def test_deleted_file_is_missing_with_history_kept(lab: Lab) -> None:
    lab.scan()
    lab.sample.analysis_r.unlink()
    report = lab.scan()
    assert report.missing == ["Analysis/densitometry_analysis.R"]
    stored = get_file(lab.conn, lab.root.id, "Analysis/densitometry_analysis.R")
    assert stored is not None and stored.status == "missing" and stored.id is not None
    assert len(text_versions(lab.conn, stored.id)) == 1
    assert lab.scan().missing == []  # reported once

    # It comes back: it's re-read and searchable again.
    lab.sample.analysis_r.write_text("# Band densitometry normalized to GAPDH\n", encoding="utf-8")
    report = lab.scan()
    assert report.new == ["Analysis/densitometry_analysis.R"]
    back = get_file(lab.conn, lab.root.id, "Analysis/densitometry_analysis.R")
    assert back is not None and back.status == "present"


def test_skipped_and_errors(lab: Lab, tmp_path: Path) -> None:
    broken = lab.sample.root / "Protocols" / "broken.docx"
    broken.write_bytes(b"PK\x03\x04 not a docx")
    report = lab.scan(Settings(size_limit_mb=1))
    big = lab.sample.root / "huge.pdf"
    with big.open("wb") as handle:
        handle.truncate(2 * 1024 * 1024)
    assert [p for p, _ in report.errors] == ["Protocols/broken.docx"]
    report = lab.scan(Settings(size_limit_mb=1))
    assert report.skipped == [("huge.pdf", "Larger than the 1 MB limit.")]
    stored = get_file(lab.conn, lab.root.id, "huge.pdf")
    assert stored is not None and stored.extract_status == "too_large"


def test_read_error_on_known_file_keeps_its_text(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    lab.scan()
    before = get_file(lab.conn, lab.root.id, "Notes/lab_meeting_2026-10-14.md")
    lab.sample.notes_md.write_text("# Changed\n", encoding="utf-8")
    from eridi.connectors.base import ConnectorError

    def locked(path: str) -> bytes:
        raise ConnectorError(f"Could not read {path}: locked")

    monkeypatch.setattr(lab.connector, "open_bytes", locked)
    report = lab.scan()
    assert report.changed == []
    assert [p for p, _ in report.errors] == ["Notes/lab_meeting_2026-10-14.md"]
    after = get_file(lab.conn, lab.root.id, "Notes/lab_meeting_2026-10-14.md")
    assert after is not None and before is not None
    assert after.text_sha256 == before.text_sha256


def test_unreachable_root_marks_nothing_missing(lab: Lab, tmp_path: Path) -> None:
    lab.scan()
    lab.connector.root = tmp_path / "unplugged"
    report = lab.scan()
    assert report.unavailable is not None and "can't be found" in report.unavailable
    assert all(f.status == "present" for f in list_files(lab.conn, lab.root.id))


def test_scan_roots_skips_roots_with_search_off(lab: Lab) -> None:
    off = Root("root_OFF", "Off", lab.root.location, search=False)
    reports = scan_roots(
        lab.conn,
        [lab.root, off],
        {lab.root.id: lab.connector, off.id: lab.connector},
        lab.snaps,
    )
    assert [r.root_id for r in reports] == [lab.root.id]
