"""M4.3: pending changes, diffs and summaries."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document

from conftest import SampleLab, entry
from eridi.connectors.local import LocalConnector
from eridi.core.changes import (
    diff,
    diff_texts,
    dismiss,
    file_diff,
    pending_changes,
    summarize,
    summarize_pair,
)
from eridi.core.config import Config, load_config, save_config
from eridi.core.index.db import open_index
from eridi.core.index.files import get_file
from eridi.core.index.records import sync_records
from eridi.core.models import EntryChange, Root, Subject
from eridi.core.record.store import RecordStore
from eridi.core.scan import scan_root
from eridi.core.snapshots import SnapshotStore

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_index(Path(":memory:"))
    yield c
    c.close()


class Lab:
    def __init__(self, sample: SampleLab, conn: sqlite3.Connection, tmp_path: Path) -> None:
        self.sample = sample
        self.conn = conn
        self.snaps = SnapshotStore()
        self.store = RecordStore.init(tmp_path / "record")
        self.root = Root("root_LAB", "Lab Drive", str(sample.root))
        self.connector = LocalConnector(sample.root, self.root.id)

    def scan(self) -> None:
        scan_root(self.conn, self.root, self.connector, self.snaps)

    def edit(self, paragraph: int, text: str) -> None:
        doc = Document(str(self.sample.protocol_docx))
        doc.paragraphs[paragraph].text = text
        doc.save(str(self.sample.protocol_docx))


@pytest.fixture
def lab(sample_lab: SampleLab, conn: sqlite3.Connection, tmp_path: Path) -> Lab:
    lab = Lab(sample_lab, conn, tmp_path)
    lab.scan()
    return lab


def test_new_files_are_not_pending(lab: Lab) -> None:
    assert pending_changes(lab.conn, lab.snaps) == []


def test_edit_creates_a_pending_change_with_summary(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    [change] = pending_changes(lab.conn, lab.snaps)
    assert change.path == PROTOCOL
    assert change.root_label == "Lab Drive"
    assert change.summary == "Primary antibody incubation: 1 h at RT → 2 h at RT"
    assert change.title == "Western blot: blocking and antibody incubation"
    assert change.from_text_sha256 != change.to_text_sha256


def test_note_with_matching_change_clears_it(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    [change] = pending_changes(lab.conn, lab.snaps)
    lab.store.append(
        entry(
            text="New lot needs longer incubation.",
            change=EntryChange(change.from_text_sha256, change.to_text_sha256, change.summary),
        )
    )
    sync_records(lab.conn, lab.store)
    assert pending_changes(lab.conn, lab.snaps) == []


def test_note_about_the_current_version_clears_it(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    [change] = pending_changes(lab.conn, lab.snaps)
    subject = Subject(lab.root.id, PROTOCOL, text_sha256=change.to_text_sha256)
    note = entry(text="Longer incubation.", subjects=[subject])
    lab.store.append(note)
    sync_records(lab.conn, lab.store)
    assert pending_changes(lab.conn, lab.snaps) == []
    # Retracting the explanation brings the change back.
    lab.store.append(entry("tombstone", "", target_id=note.id))
    sync_records(lab.conn, lab.store)
    assert len(pending_changes(lab.conn, lab.snaps)) == 1


def test_an_older_explanation_does_not_clear_a_newer_change(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    [first] = pending_changes(lab.conn, lab.snaps)
    lab.store.append(entry(text="why", change=EntryChange(None, first.to_text_sha256)))
    sync_records(lab.conn, lab.store)
    lab.edit(2, "Primary antibody incubation: overnight at 4 °C.")
    lab.scan()
    [second] = pending_changes(lab.conn, lab.snaps)
    assert second.from_text_sha256 == first.to_text_sha256
    assert second.summary == "Primary antibody incubation: 2 h at RT → overnight at 4 °C"


def test_dismiss_persists_across_restarts(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    [change] = pending_changes(lab.conn, lab.snaps)
    config = dismiss(Config.default(), change)
    assert dismiss(config, change) == config  # idempotent
    save_config(config)
    reloaded = load_config()
    assert reloaded.dismissed_changes == (change.key,)
    assert pending_changes(lab.conn, lab.snaps, reloaded.dismissed_changes) == []


def test_docx_table_change_summary_is_readable(lab: Lab) -> None:
    doc = Document(str(lab.sample.protocol_docx))
    doc.tables[0].cell(1, 1).text = "1:500"
    doc.save(str(lab.sample.protocol_docx))
    lab.scan()
    [change] = pending_changes(lab.conn, lab.snaps)
    assert change.summary == "Anti-pERK (lot B2291): 1:1000 → 1:500"


def test_file_diff_defaults_to_previous_version(lab: Lab) -> None:
    lab.edit(2, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    file = get_file(lab.conn, lab.root.id, PROTOCOL)
    assert file is not None
    d = file_diff(lab.conn, lab.snaps, file)
    assert d.available
    assert ("-", "Primary antibody incubation: 1 h at RT.") in [(ln.op, ln.text) for ln in d.lines]
    assert ("+", "Primary antibody incubation: 2 h at RT.") in [(ln.op, ln.text) for ln in d.lines]
    assert d.added == 1 and d.removed == 1
    assert "- Primary antibody incubation: 1 h at RT." in d.unified


def test_diff_with_evicted_snapshot_is_unavailable(lab: Lab) -> None:
    to_sha = lab.snaps.put("new")
    assert not diff(lab.snaps, "0" * 64, to_sha).available
    assert diff(lab.snaps, None, to_sha).available  # against nothing


def test_diff_groups_distant_changes() -> None:
    old = "\n".join(f"line {i}" for i in range(30))
    new = old.replace("line 2\n", "line two\n").replace("line 27", "line twenty-seven")
    lines, summary = diff_texts(old, new)
    assert [ln.op for ln in lines].count("@") == 1
    assert summary == "line 2 → line two"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("Primary antibody incubation: 1 h at RT.", "Primary antibody incubation: 2 h at RT.",
         "Primary antibody incubation: 1 h at RT → 2 h at RT"),
        ("Anti-GAPDH | G1150 | 2026-06-02", "Anti-GAPDH | G1200 | 2026-11-01",
         "Anti-GAPDH: G1150 | 2026-06-02 → G1200 | 2026-11-01"),
        ("Block in milk.", "Block in BSA.", "Block in milk → Block in BSA"),
        ("x" * 100, "y", "x" * 59 + "… → y"),
    ],
)  # fmt: skip
def test_summarize_pair(old: str, new: str, expected: str) -> None:
    assert summarize_pair(old, new) == expected


def test_summarize_inserts_and_deletes() -> None:
    assert summarize(["a"], ["a", "Wash 3 × 5 min in TBST"]) == "Added: Wash 3 × 5 min in TBST"
    assert summarize(["a", "Old step"], ["a"]) == "Removed: Old step"
    assert summarize(["a"], ["a"]) is None


def test_summary_skips_blank_lines() -> None:
    assert summarize(["a"], ["a", "", "[Comment] new"]) == "Added: [Comment] new"
    assert summarize(["a", "", "gone"], ["a"]) == "Removed: gone"
    assert summarize(["a"], ["a", ""]) is None
