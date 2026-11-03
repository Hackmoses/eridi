"""M9.1: a lab shares one record folder; each member sees everyone's entries."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.core.service import Eridi, EridiError

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@contextmanager
def member(home: Path) -> Iterator[Eridi]:
    """One lab member's computer: their own app data, config and index."""
    old = os.environ.get("ERIDI_HOME")
    os.environ["ERIDI_HOME"] = str(home)
    try:
        with Eridi() as eridi:
            yield eridi
    finally:
        if old is not None:
            os.environ["ERIDI_HOME"] = old


def test_two_members_writing_to_one_record_folder(tmp_path: Path, sample_lab: SampleLab) -> None:
    shared = tmp_path / "SharePoint - Lab" / "Eridi"
    with member(tmp_path / "okafor") as okafor:
        okafor.init("A. Okafor", "a@example.edu", shared)
        okafor.add_root(sample_lab.root, "Lab Drive", mark_folder=True)
        okafor.scan()
        first = okafor.add_note("Switched to BSA for phospho blots.", file=PROTOCOL)

    with member(tmp_path / "lee") as lee:
        lee.init("B. Lee", None, shared, join=True)
        assert [e.id for e in lee.recent()] == [first.id]
        lee.add_root(sample_lab.root)  # the marker gives the same storage ID
        lee.scan()
        reply = lee.add_note("BSA works for the pERK blots too.", file=PROTOCOL)
        history = lee.file_history(PROTOCOL)
        assert [i.entry.author.name for i in history.entries] == ["A. Okafor", "B. Lee"]
        assert [m[0] for m in lee.record_members()] == ["B. Lee", "A. Okafor"]

    with member(tmp_path / "okafor") as okafor:
        assert [e.id for e in okafor.recent()] == [reply.id, first.id]
        assert {h.id for h in okafor.search("pERK BSA") if h.kind == "entry"} >= {reply.id}
        # Each entry is its own file, so concurrent writers never touch the same file.
        files = sorted(p.name for p in (shared / "entries").rglob("*.json"))
        assert files == sorted([f"{first.id}.json", f"{reply.id}.json"])


def test_join_needs_an_existing_record_folder(tmp_path: Path) -> None:
    with Eridi() as eridi:
        with pytest.raises(EridiError, match="can't be found"):
            eridi.init("B. Lee", None, tmp_path / "missing", join=True)
        (tmp_path / "Lab").mkdir()
        with pytest.raises(EridiError, match="not an Eridi record folder"):
            eridi.init("B. Lee", None, tmp_path / "Lab", join=True)
        assert not (tmp_path / "Lab" / "eridi-record.json").exists()  # nothing created
        assert not eridi.initialized

        with member(tmp_path / "okafor") as okafor:
            okafor.init("A. Okafor", None, tmp_path / "Lab" / "Eridi")
        with pytest.raises(EridiError, match=r"seems to be in .*Eridi"):
            eridi.init("B. Lee", None, tmp_path / "Lab", join=True)
        eridi.init("B. Lee", None, tmp_path / "Lab" / "Eridi", join=True)
        assert eridi.initialized


def test_switching_record_folders_drops_the_old_entries(tmp_path: Path) -> None:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "mine")
        eridi.add_note("Only in my own record.")
        with member(tmp_path / "lee") as lee:
            lee.init("B. Lee", None, tmp_path / "lab")
            shared = lee.add_note("In the lab record.")
        eridi.init("A. Okafor", None, tmp_path / "lab", join=True)
        assert [e.id for e in eridi.recent()] == [shared.id]


def test_init_join_command(tmp_path: Path) -> None:
    runner = CliRunner()
    with member(tmp_path / "okafor") as okafor:
        okafor.init("A. Okafor", None, tmp_path / "lab")
        okafor.add_note("First lab note.")
    result = runner.invoke(
        cli, ["init", "--name", "B. Lee", "--email", "", "--join", str(tmp_path / "lab")]
    )
    assert result.exit_code == 0, result.output
    assert "1 entries from A. Okafor" in result.output
    bad = runner.invoke(
        cli, ["init", "--name", "B. Lee", "--email", "", "--join", str(tmp_path / "nope")]
    )
    assert bad.exit_code == 1 and "can't be found" in bad.output
    both = runner.invoke(cli, ["init", "--name", "x", "--join", "a", "--record-folder", "b"])
    assert both.exit_code == 2
