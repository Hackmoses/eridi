"""M4.4: the Eridi service, end to end."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document

from conftest import SampleLab
from eridi.core.scan import ScanProgress
from eridi.core.service import (
    AmbiguousFileError,
    Eridi,
    EridiError,
    FileNotFoundInIndexError,
    NotInitializedError,
    UnknownEntryError,
    file_id,
)

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def app() -> Iterator[Eridi]:
    with Eridi() as eridi:
        yield eridi


@pytest.fixture
def lab(app: Eridi, sample_lab: SampleLab, tmp_path: Path) -> Eridi:
    app.init("A. Okafor", "a.okafor@example.edu", tmp_path / "OneDrive" / "Eridi")
    app.add_root(sample_lab.root, "Lab Drive")
    app.scan()
    return app


def edit_protocol(sample_lab: SampleLab, text: str) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = text
    doc.save(str(sample_lab.protocol_docx))


def test_end_to_end(app: Eridi, sample_lab: SampleLab, tmp_path: Path) -> None:
    app.init("A. Okafor", "a.okafor@example.edu", tmp_path / "record")
    root = app.add_root(sample_lab.root, "Lab Drive")
    assert [r.label for r in app.list_roots()] == ["Lab Drive"]

    first = app.scan()
    assert first.total("new") == 9
    assert app.pending_changes() == []

    edit_protocol(sample_lab, "Primary antibody incubation: 2 h at RT.")
    second = app.scan()
    assert second.total("changed") == 1

    [change] = app.pending_changes()
    assert change.path == PROTOCOL
    assert change.summary == "Primary antibody incubation: 1 h at RT → 2 h at RT"
    diff = app.diff(PROTOCOL)
    assert ("+", "Primary antibody incubation: 2 h at RT.") in [(d.op, d.text) for d in diff.lines]

    note = app.add_note("The new antibody lot needs a longer incubation.", file="blocking_v7")
    assert note.change is not None and note.change.to_text_sha256 == change.to_text_sha256
    assert note.subjects[0].root_label == "Lab Drive"
    assert app.pending_changes() == []

    hits = app.search("longer incubation")
    assert hits[0].kind == "entry" and hits[0].id == note.id

    history = app.file_history(f"{root.id}:{PROTOCOL}")
    assert len(history.versions) == 2
    assert [v.is_current for v in history.versions] == [False, True]
    assert all(v.available for v in history.versions)
    assert [i.entry.id for i in history.entries] == [note.id]
    assert history.root_label == "Lab Drive"

    # The note is in the user's record folder, as a plain JSON file.
    assert list((tmp_path / "record" / "entries").rglob(f"{note.id}.json"))


def test_outcome_retract_and_recent(lab: Eridi) -> None:
    note = lab.add_note("Switched to BSA.", file=PROTOCOL, tags=["Blocking"])
    outcome = lab.add_outcome("worked", entry_id=note.id, text="Clean background.")
    assert outcome.target_id == note.id
    assert [e.id for e in lab.recent()] == [outcome.id, note.id]
    assert [e.id for e in lab.recent(kind="note")] == [note.id]
    history = lab.file_history(PROTOCOL)
    assert {i.entry.id for i in history.entries} == {note.id, outcome.id}
    lab.retract(note.id, "wrong file")
    assert [e.id for e in lab.recent()] == [outcome.id]
    assert lab.entry_state(note.id) == "retracted"
    assert lab.get_entry(note.id.lower()).text == "Switched to BSA."


def test_correcting_a_note(lab: Eridi) -> None:
    note = lab.add_note("Incubate 1 h.", file=PROTOCOL)
    fixed = lab.add_note("Incubate 2 h.", file=PROTOCOL, supersedes=note.id)
    assert [e.id for e in lab.recent()] == [fixed.id]
    states = {i.entry.id: i.state for i in lab.file_history(PROTOCOL).entries}
    assert states == {note.id: "superseded", fixed.id: "current"}
    with pytest.raises(UnknownEntryError):
        lab.add_note("x", supersedes="01JC8Z9K3V5W6X7Y8Z9A0B1C2D")


def test_dismiss_survives_restart(lab: Eridi, sample_lab: SampleLab) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 3 h at RT.")
    lab.scan()
    [change] = lab.pending_changes()
    lab.dismiss(change)
    lab.close()
    with Eridi() as again:
        assert again.pending_changes() == []


def test_history_follows_a_rename(lab: Eridi, sample_lab: SampleLab) -> None:
    note = lab.add_note("About the protocol.", file=PROTOCOL)
    sample_lab.protocol_docx.rename(sample_lab.protocol_docx.with_name("WB_v8.docx"))
    summary = lab.scan()
    assert summary.reports[0].moved == [(PROTOCOL, "Protocols/WB_v8.docx")]
    history = lab.file_history("WB_v8")
    assert [i.entry.id for i in history.entries] == [note.id]
    assert history.moves[0][:2] == (PROTOCOL, "Protocols/WB_v8.docx")


def test_resolve_file(lab: Eridi, sample_lab: SampleLab) -> None:
    root = lab.list_roots()[0]
    by_id = lab.resolve_file(f"{root.id}:{PROTOCOL}")
    assert file_id(by_id) == f"{root.id}:{PROTOCOL}"
    assert lab.resolve_file(PROTOCOL) == by_id
    assert lab.resolve_file(str(sample_lab.protocol_docx)) == by_id  # a path on disk
    assert lab.resolve_file("protocols\\western_blot_blocking_v7.docx") == by_id
    assert lab.resolve_file("Western_blot_blocking_v7.docx") == by_id
    assert lab.resolve_file("western_blot_blocking_v7") == by_id
    assert lab.resolve_file("blocking_v7") == by_id
    assert lab.resolve_file("Färbung").path == "Protokolle_Ü/Färbung ñ v2.md"
    with pytest.raises(AmbiguousFileError) as info:
        lab.resolve_file("Papers/")
    assert len(info.value.candidates) == 2
    with pytest.raises(FileNotFoundInIndexError, match="eridi scan"):
        lab.resolve_file("no such protocol")
    assert [f.path for f in lab.find_files("papers")] == [
        "Papers/Blocking_buffer_comparison.pdf",
        "Papers/Scanned_notebook_page.pdf",
    ]


def test_writes_need_init(app: Eridi) -> None:
    assert not app.initialized
    with pytest.raises(NotInitializedError, match="eridi init"):
        app.add_note("why")


def test_clear_errors(lab: Eridi, sample_lab: SampleLab, tmp_path: Path) -> None:
    with pytest.raises(EridiError, match="empty"):
        lab.add_note("   ")
    with pytest.raises(EridiError, match="Outcome must be one of"):
        lab.add_note("x", outcome="great")
    with pytest.raises(EridiError, match="either"):
        lab.add_outcome("worked")
    with pytest.raises(EridiError, match="already connected"):
        lab.add_root(sample_lab.root)
    with pytest.raises(EridiError, match="overlaps"):
        lab.add_root(sample_lab.root / "Protocols")
    with pytest.raises(EridiError, match="not a folder"):
        lab.add_root(tmp_path / "nope")
    with pytest.raises(UnknownEntryError):
        lab.get_entry("01JC8Z9K3V5W6X7Y8Z9A0B1C2D")


def test_folder_marker_only_when_asked(app: Eridi, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    app.add_root(plain)
    assert list(plain.iterdir()) == []  # nothing written into the user's folder
    marked = tmp_path / "marked"
    marked.mkdir()
    root = app.add_root(marked, "Shared drive", mark_folder=True)
    assert (marked / ".eridi-root.json").is_file()
    # Another computer adding the same synced folder gets the same storage ID.
    app.remove_root(root.id)
    again = app.add_root(marked)
    assert again.id == root.id and again.label == "Shared drive"


def test_update_and_remove_root(lab: Eridi) -> None:
    root = lab.list_roots()[0]
    lab.update_root(root.id, search=False)
    assert lab.search("milk") == []  # storage with search off isn't searched
    lab.update_root(root.id, search=True, label="Lab OneDrive")
    assert lab.search("milk")[0].root_label == "Lab OneDrive"
    lab.remove_root(root.id)
    assert lab.list_roots() == []
    assert lab.search("milk") == []


def test_record_folder_inside_a_root_is_not_indexed(app: Eridi, sample_lab: SampleLab) -> None:
    app.init("A", record_folder=sample_lab.root / "Eridi record")
    app.add_root(sample_lab.root)
    app.scan()
    app.add_note("A note in the record.")
    app.scan()
    assert all(not (h.path or "").startswith("Eridi record") for h in app.search("record"))


def test_fresh_index_rebuilds_on_first_scan(
    lab: Eridi, sample_lab: SampleLab, isolated_home: Path
) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 4 h at RT.")
    lab.scan()
    note = lab.add_note("why", file=PROTOCOL)
    lab.close()
    (isolated_home / "index.sqlite").unlink()
    for extra in ("index.sqlite-wal", "index.sqlite-shm"):
        (isolated_home / extra).unlink(missing_ok=True)
    with Eridi() as again:
        summary = again.scan()
        assert summary.rebuilt
        history = again.file_history(PROTOCOL)
        assert len(history.versions) == 2  # restored from the version log
        assert [i.entry.id for i in history.entries] == [note.id]


def test_doctor(lab: Eridi, sample_lab: SampleLab) -> None:
    report = lab.doctor()
    assert report.ok
    names = {c.name for c in report.checks}
    assert {"Search engine", "Setup", "Record folder", "Storage 'Lab Drive'", "Snapshots"} <= names
    assert any("scans without a text layer" in c.detail for c in report.checks)
    record_file = next(lab.store.folder.rglob("*.json"))
    lab.add_note("one")
    entry_file = next((lab.store.folder / "entries").rglob("*.json"))
    entry_file.with_name(entry_file.stem + " (1).json").write_bytes(entry_file.read_bytes())
    report = lab.doctor()
    assert any(c.name == "Record file ignored" for c in report.checks)
    assert record_file.exists()


def test_doctor_reports_missing_storage(lab: Eridi, tmp_path: Path) -> None:
    gone = tmp_path / "usb"
    gone.mkdir()
    lab.add_root(gone, "USB")
    gone.rmdir()
    report = lab.doctor()
    assert not report.ok
    assert any(c.status == "error" and "USB" in c.name for c in report.checks)
    assert lab.scan().reports[-1].unavailable is not None


def test_sees_storage_added_by_another_eridi(app: Eridi, sample_lab: SampleLab) -> None:
    """The web app keeps running while `eridi storage add` runs in a terminal."""
    app.init("A. Okafor")
    assert app.list_roots() == []
    with Eridi() as terminal:
        terminal.add_root(sample_lab.root, "Lab Drive")
    assert [r.label for r in app.list_roots()] == ["Lab Drive"]
    assert app.scan().total("new") == 9


def test_scan_reports_progress(lab: Eridi, sample_lab: SampleLab) -> None:
    edit_protocol(sample_lab, "Primary antibody incubation: 9 h at RT.")
    seen: list[ScanProgress] = []
    lab.scan(progress=seen.append)
    reading = [p for p in seen if p.phase == "reading"]
    assert len(reading) == 1 and reading[0].root.label == "Lab Drive"  # only the edited file


def test_search_works_while_a_scan_runs(
    lab: Eridi, sample_lab: SampleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long scan in the app's background thread must not freeze search."""
    import threading

    import eridi.core.service as service_module

    started, release = threading.Event(), threading.Event()
    real_scan_root = service_module.scan_root

    def slow_scan_root(*args: object, **kwargs: object) -> object:
        started.set()
        release.wait(10)
        return real_scan_root(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module, "scan_root", slow_scan_root)
    scanning = threading.Thread(target=lab.scan)
    scanning.start()
    assert started.wait(5)
    hits = lab.search("blocking buffer")  # would hang if the scan held the lock
    assert hits
    release.set()
    scanning.join(10)
    assert not scanning.is_alive()
