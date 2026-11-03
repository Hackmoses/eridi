"""M8.2: `eridi project …` commands."""

from pathlib import Path

import pytest

from conftest import SampleLab
from test_cli import edit_protocol, fails, ok

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> SampleLab:
    shared = tmp_path / "Shared"
    (shared / "Imaging").mkdir(parents=True)
    (shared / "Imaging" / "confocal.md").write_text("Laser power for stains.\n", "utf-8")
    ok("init", "--name", "A. Okafor", "--email", "", "--record-folder", str(tmp_path / "record"))
    ok("storage", "add", str(sample_lab.root), "--label", "Lab Drive")
    ok("storage", "add", str(shared), "--label", "Shared")
    ok("scan")
    return sample_lab


def test_project_commands(lab: SampleLab, tmp_path: Path) -> None:
    assert "No projects yet" in ok("project", "list")
    out = ok("project", "new", "Western blots", "--folder", str(lab.root / "Protocols"))
    assert "Western blots" in out and "Lab Drive / Protocols" in out
    out = ok("project", "add-folder", "western", str(tmp_path / "Shared" / "Imaging"))
    assert "Shared / Imaging" in out
    out = ok("project", "rename", "western", "Western blots 2026")
    assert "Western blots 2026" in out
    assert "Saves to Shared" in ok("project", "home", "western", "Shared")
    listed = ok("project", "list")
    assert "Western blots 2026" in listed and "Lab Drive / Protocols" in listed

    edit_protocol(lab, "Primary antibody incubation: 2 h at RT.")
    ok("scan")
    shown = ok("project", "show", "western")
    assert PROTOCOL in shown and "Imaging/confocal.md" in shown
    assert "Needs a why" in shown and "1 h at RT → 2 h at RT" in shown
    assert "Notes/lab_meeting" not in shown

    ok("why", PROTOCOL, "New antibody lot needs longer.")
    shown = ok("project", "show", "western")
    assert "Needs a why" not in shown
    assert "New antibody lot needs longer." in shown

    out = ok("project", "remove-folder", "western", str(tmp_path / "Shared" / "Imaging"))
    assert "Shared / Imaging" not in out


def test_project_errors(lab: SampleLab) -> None:
    assert "No project matches" in fails("project", "show", "nothing")
    ok("project", "new", "Western blots")
    assert "already a project" in fails("project", "new", "western blots")
    assert "isn't inside any connected storage" in fails(
        "project", "add-folder", "western", "/not/a/folder"
    )
    assert "No single storage" in fails("project", "home", "western", "Dropbox")
    assert "Usage" in ok("project", "new", "--help")
