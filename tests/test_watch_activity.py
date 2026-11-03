"""Live watching and the activity feed (pulled forward from M9 at the user's request)."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.core.service import Eridi
from eridi.core.watch import Watcher

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        yield eridi


def edit_protocol(sample_lab: SampleLab, text: str) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = text
    doc.save(str(sample_lab.protocol_docx))


def root_id(eridi: Eridi) -> str:
    return eridi.list_roots()[0].id


# --- scanning just the files a watcher reports ---


def test_scan_changed_handles_edits_deletes_renames_and_ignores(
    lab: Eridi, sample_lab: SampleLab
) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    sample_lab.analysis_r.unlink()
    sample_lab.notes_md.rename(sample_lab.notes_md.with_name("meeting renamed.md"))
    (sample_lab.root / "Protocols" / "~$temp.docx").write_bytes(b"lock")
    report = lab.scan_changed(
        root_id(lab),
        [
            PROTOCOL,
            "Analysis/densitometry_analysis.R",
            "Notes/lab_meeting_2026-10-14.md",
            "Notes/meeting renamed.md",
            "Protocols/~$temp.docx",
        ],
    )
    assert report is not None
    assert report.changed == [PROTOCOL]
    assert report.missing == ["Analysis/densitometry_analysis.R"]
    assert report.moved == [("Notes/lab_meeting_2026-10-14.md", "Notes/meeting renamed.md")]
    assert report.new == []
    assert len(lab.pending_changes()) == 1


# --- activity feed ---


def test_first_scan_is_the_baseline_not_activity(lab: Eridi) -> None:
    assert lab.activity() == []


def test_edits_and_new_files_show_newest_first(lab: Eridi, sample_lab: SampleLab) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    (sample_lab.root / "Notes" / "new idea.md").write_text("# New idea\nTry PVDF.\n", "utf-8")
    lab.scan()
    items = lab.activity()
    assert [(a.kind, a.path) for a in items] == [
        ("added", "Notes/new idea.md"),
        ("edited", PROTOCOL),
    ]
    edited = items[1]
    assert edited.summary == "Primary antibody incubation: 1 h at RT → 2 h at RT"
    assert edited.needs_why and edited.is_current and edited.root_label == "Lab Drive"

    note = lab.add_note("Longer for the new lot.", file=PROTOCOL)
    edited = next(a for a in lab.activity() if a.kind == "edited")
    assert not edited.needs_why
    assert [e.entry_id for e in edited.explanations] == [note.id]


def test_older_edits_stay_in_activity(lab: Eridi, sample_lab: SampleLab) -> None:
    for hours in (2, 3):
        edit_protocol(sample_lab, f"Primary antibody incubation: {hours} h at RT.")
        lab.scan()
    items = [a for a in lab.activity() if a.path == PROTOCOL]
    assert [a.summary for a in items] == [
        "Primary antibody incubation: 2 h at RT → 3 h at RT",
        "Primary antibody incubation: 1 h at RT → 2 h at RT",
    ]
    assert [a.is_current for a in items] == [True, False]
    assert [a.needs_why for a in items] == [True, False]  # only the current one is pending


def test_a_new_folder_is_its_own_baseline(lab: Eridi, tmp_path: Path) -> None:
    other = tmp_path / "Other"
    other.mkdir()
    (other / "old.md").write_text("already here\n", "utf-8")
    lab.add_root(other, "Other")
    lab.scan()
    assert lab.activity() == []


# --- the watcher ---


def wait_for(condition: object, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return True
        time.sleep(0.1)
    return False


def test_watcher_records_a_saved_file_within_seconds(lab: Eridi, sample_lab: SampleLab) -> None:
    seen: list[str] = []
    watcher = Watcher(lab, debounce=0.3, on_report=lambda root, r: seen.extend(r.changed))
    watcher.start()
    try:
        assert watcher.watching == [root_id(lab)]
        time.sleep(0.5)  # let the OS watch settle
        edit_protocol(sample_lab, "Primary antibody incubation: overnight at 4 °C.")
        assert wait_for(lambda: PROTOCOL in seen), "the save was not noticed"
        [change] = lab.pending_changes()
        assert change.summary == "Primary antibody incubation: 1 h at RT → overnight at 4 °C"
        assert lab.last_activity is not None
    finally:
        watcher.stop()


def test_watcher_follows_storage_changes(lab: Eridi, tmp_path: Path) -> None:
    watcher = Watcher(lab, debounce=0.3)
    watcher.start()
    try:
        lab.update_root(root_id(lab), search=False)
        watcher._sync_watches()
        assert watcher.watching == []
    finally:
        watcher.stop()


def test_note_and_flush_group_paths(lab: Eridi, sample_lab: SampleLab) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 9 h at RT.")
    watcher = Watcher(lab)
    watcher.note(root_id(lab), PROTOCOL)
    watcher.note(root_id(lab), PROTOCOL)
    [(rid, report)] = watcher.flush()
    assert rid == root_id(lab) and report.changed == [PROTOCOL]
    assert watcher.flush() == []


# --- interfaces ---


def test_activity_command(lab: Eridi, sample_lab: SampleLab) -> None:
    runner = CliRunner()
    assert "No edits yet" in runner.invoke(cli, ["activity"]).output
    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    out = runner.invoke(cli, ["activity"]).output
    assert "edited  Protocols/Western_blot_blocking_v7.docx" in out
    assert "1 h at RT → 2 h at RT" in out
    assert "needs a why" in out


def test_activity_api(lab: Eridi, sample_lab: SampleLab) -> None:
    from fastapi.testclient import TestClient

    from eridi.api.app import create_app
    from eridi.api.scanner import BackgroundScanner

    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    app = create_app(lab, token="t", port=1, scanner=BackgroundScanner(lab, 0), watch=False)  # noqa: S106
    with TestClient(app, base_url="http://127.0.0.1:1") as client:
        headers = {"Authorization": "Bearer t"}
        [item] = client.get("/api/activity", headers=headers).json()["activity"]
        assert item["kind"] == "edited" and item["needs_why"]
        assert item["summary"] == "Primary antibody incubation: 1 h at RT → 2 h at RT"
        diff = client.get(
            "/api/diff",
            params={"file": item["file_id"], "from": item["from"], "to": item["to"]},
            headers=headers,
        ).json()
        assert {"op": "+", "text": "Primary antibody incubation: 2 h at RT."} in diff["lines"]
        assert client.get("/api/status", headers=headers).json()["last_activity"] is not None
