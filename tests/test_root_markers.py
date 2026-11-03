"""M9.3: the same synced folder, at different paths on different computers, is one root."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.connectors.local import ROOT_MARKER
from eridi.core.service import Eridi, EridiError
from test_shared_record import member

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


def synced_copy(source: Path, target: Path) -> Path:
    """What a sync app does: the same folder appears on the other computer, elsewhere."""
    shutil.copytree(source, target)
    return target


def test_same_root_at_two_paths_resolves_to_one_root_id(
    tmp_path: Path, sample_lab: SampleLab
) -> None:
    record = tmp_path / "shared-record"
    with member(tmp_path / "mac") as mac:
        mac.init("A. Okafor", None, record)
        root = mac.add_root(sample_lab.root, "Lab OneDrive", mark_folder=True)
        mac.scan()
        note = mac.add_note("Switched to BSA.", file=PROTOCOL)
    assert (sample_lab.root / ROOT_MARKER).is_file()

    elsewhere = synced_copy(sample_lab.root, tmp_path / "C" / "Users" / "lee" / "OneDrive - Lab")
    with member(tmp_path / "windows") as win:
        win.init("B. Lee", None, record, join=True)
        same = win.add_root(elsewhere)
        assert same.id == root.id
        assert same.label == "Lab OneDrive"  # from the marker
        win.scan()
        assert [i.entry.id for i in win.file_history(PROTOCOL).entries] == [note.id]
        assert win.pending_changes() == []


def test_unmarked_roots_get_different_ids(tmp_path: Path, sample_lab: SampleLab) -> None:
    with member(tmp_path / "mac") as mac:
        mac.init("A. Okafor", None, tmp_path / "rec")
        first = mac.add_root(sample_lab.root)
    assert not (sample_lab.root / ROOT_MARKER).exists()  # nothing written without asking
    copy = synced_copy(sample_lab.root, tmp_path / "copy")
    with member(tmp_path / "win") as win:
        win.init("B. Lee", None, tmp_path / "rec", join=True)
        assert win.add_root(copy).id != first.id


def test_marking_later_and_adopting_a_labmates_id(tmp_path: Path, sample_lab: SampleLab) -> None:
    record = tmp_path / "rec"
    copy = synced_copy(sample_lab.root, tmp_path / "copy")
    with member(tmp_path / "win") as win:
        win.init("B. Lee", None, record)
        mine = win.add_root(copy, "Lab")  # added before anyone marked it
        win.scan()
    with member(tmp_path / "mac") as mac:
        mac.init("A. Okafor", None, record, join=True)
        theirs = mac.add_root(sample_lab.root, "Lab")
        assert mac.mark_root(theirs.id) == theirs  # writes the marker
        assert mac.mark_root(theirs.id) == theirs  # and again is harmless
        mac.scan()
        note = mac.add_note("Antibody lot changed.", file=PROTOCOL)
    shutil.copy2(sample_lab.root / ROOT_MARKER, copy / ROOT_MARKER)  # sync delivers it

    with member(tmp_path / "win") as win:
        assert win.root_marker(mine.id) == theirs.id
        switched = win.mark_root(mine.id)
        assert switched.id == theirs.id
        assert [r.id for r in win.list_roots()] == [theirs.id]
        assert switched.location == mine.location and switched.label == "Lab"
        win.scan()
        assert [i.entry.id for i in win.file_history(PROTOCOL).entries] == [note.id]
        # The marker itself is never indexed as a lab file.
        assert ROOT_MARKER not in {f.path for f in win.find_files("eridi")}


def test_mark_errors(
    tmp_path: Path, sample_lab: SampleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eridi.core.service as service

    def read_only(*args: object) -> None:
        raise PermissionError(13, "Permission denied")

    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "rec")
        root = eridi.add_root(sample_lab.root)
        monkeypatch.setattr(service, "write_root_marker", read_only)
        with pytest.raises(EridiError, match="Could not write the folder marker"):
            eridi.mark_root(root.id)
        other = synced_copy(sample_lab.root, tmp_path / "second")
        second = eridi.add_root(other)
        (other / ROOT_MARKER).write_text(f'{{"root_id": "{root.id}"}}', "utf-8")
        with pytest.raises(EridiError, match="already connected here at another path"):
            eridi.mark_root(second.id)


def test_storage_mark_and_add_commands(tmp_path: Path, sample_lab: SampleLab) -> None:
    runner = CliRunner()

    def ok(*args: str) -> str:
        result = runner.invoke(cli, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    ok("init", "--name", "A. Okafor", "--email", "", "--record-folder", str(tmp_path / "rec"))
    ok("storage", "add", str(sample_lab.root), "--label", "Lab")  # not a terminal: no question
    assert not (sample_lab.root / ROOT_MARKER).exists()
    assert "marked" not in ok("storage", "list")
    assert "is marked" in ok("storage", "mark", "Lab")
    assert "marked" in ok("storage", "list")

    copy = synced_copy(sample_lab.root, tmp_path / "copy")
    with member(tmp_path / "other"):
        ok("init", "--name", "B. Lee", "--email", "", "--join", str(tmp_path / "rec"))
        assert "Recognized from its folder marker" in ok("storage", "add", str(copy))
        other = tmp_path / "other-folder"
        other.mkdir()
        ok("storage", "add", str(other), "--mark")
        assert (other / ROOT_MARKER).is_file()


def test_mark_from_the_web_app(tmp_path: Path, sample_lab: SampleLab) -> None:
    from fastapi.testclient import TestClient

    from eridi.api.app import create_app
    from eridi.api.scanner import BackgroundScanner
    from test_api import AUTH, BASE, PORT, TOKEN

    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "rec")
        root = eridi.add_root(sample_lab.root, "Lab")
        app = create_app(eridi, token=TOKEN, port=PORT, scanner=BackgroundScanner(eridi, 0))
        with TestClient(app, base_url=BASE, headers=AUTH) as client:
            [listed] = client.get("/api/storage").json()["storage"]
            assert listed["marked"] is False
            marked = client.post(f"/api/storage/{root.id}/mark")
            assert marked.status_code == 200 and marked.json()["marked"] is True
            assert client.get("/api/storage").json()["storage"][0]["marked"] is True
            assert client.post("/api/storage/root_nope/mark").status_code == 400
