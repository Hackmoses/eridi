"""M9.4: `eridi doctor` explains what sync tools did to a shared record."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.connectors.local import ROOT_MARKER
from eridi.core.record.store import conflict_original
from eridi.core.service import DoctorReport, Eridi

ULID = "01JC8Z9K3V5W6X7Y8Z9A0B1C2D"


@pytest.mark.parametrize(
    "name",
    [
        f"{ULID} (1).json",
        f"{ULID}-DESKTOP-ABC.json",
        f"{ULID}-LAPTOP-7.json",
        f"{ULID} (conflicted copy 2026-11-03).json",
        f"{ULID.lower()} (2).json",
    ],
)
def test_conflict_copy_names(name: str) -> None:
    assert conflict_original(Path(name)) == ULID


@pytest.mark.parametrize("name", [f"{ULID}.json", "notes.json", f"{ULID}0.json", "x (1).json"])
def test_not_conflict_copies(name: str) -> None:
    assert conflict_original(Path(name)) is None


def details(report: DoctorReport, name: str) -> list[str]:
    return [c.detail for c in report.checks if c.name == name]


def test_doctor_reports_conflict_copies_and_newer_entries(tmp_path: Path) -> None:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "rec")
        kept = eridi.add_note("Intact note.")
        lost = eridi.add_note("Note whose original was lost.")
        kept_file = eridi.store.path_for(kept)
        lost_file = eridi.store.path_for(lost)
        shutil.copy2(kept_file, kept_file.with_name(f"{kept.id}-DESKTOP-ABC.json"))
        lost_file.rename(lost_file.with_name(f"{lost.id} (1).json"))
        shutil.copy2(
            eridi.store.folder / "eridi-record.json",
            eridi.store.folder / "eridi-record (1).json",
        )
        newer = json.loads(kept_file.read_text("utf-8"))
        newer.update(schema="eridi.record/2", id="01" + "Z" * 24)
        (kept_file.parent / f"{newer['id']}.json").write_text(json.dumps(newer), "utf-8")
        (kept_file.parent / "stray.txt").write_text("?", "utf-8")

        report = eridi.doctor()
        assert report.ok  # warnings, not errors
        copies = details(report, "Sync conflict copy")
        assert len(copies) == 3
        assert any("DESKTOP-ABC" in d and "intact" in d and "delete" in d for d in copies)
        assert any(
            f"{lost.id} (1).json" in d and f"Rename it to {lost.id}.json" in d for d in copies
        )
        assert any("eridi-record (1).json" in d for d in copies)
        [newer_detail] = details(report, "Newer entries")
        assert "1 entries" in newer_detail and "Update Eridi" in newer_detail
        assert any("stray.txt" in d for d in details(report, "Record file ignored"))

        # Following the advice restores the lost entry.
        lost_file.with_name(f"{lost.id} (1).json").rename(lost_file)
        assert lost.id in {e.id for e in eridi.recent()}
        assert not any(lost.id in d for d in details(eridi.doctor(), "Sync conflict copy"))


def test_doctor_reports_a_storage_id_mismatch(tmp_path: Path, sample_lab: SampleLab) -> None:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "rec")
        root = eridi.add_root(sample_lab.root, "Lab")
        (sample_lab.root / ROOT_MARKER).write_text(
            json.dumps({"root_id": "root_01JC8W0000000000000000000A", "label": "Lab"}), "utf-8"
        )
        [warning] = [
            c for c in eridi.doctor().checks if c.name == "Storage 'Lab'" and c.status != "ok"
        ]
        assert f"eridi storage mark {root.id}" in warning.detail
        eridi.mark_root(root.id)
        assert all(c.status == "ok" for c in eridi.doctor().checks if c.name == "Storage 'Lab'")


def test_doctor_command_shows_conflicts(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["init", "--name", "A. Okafor", "--email", "", "--record-folder", str(tmp_path / "r")]
    )
    assert result.exit_code == 0, result.output
    with Eridi() as eridi:
        entry = eridi.add_note("x")
        path = eridi.store.path_for(entry)
        shutil.copy2(path, path.with_name(f"{entry.id} (1).json"))
    out = runner.invoke(cli, ["doctor"]).output
    assert "Sync conflict copy" in out and "you can delete it" in out
