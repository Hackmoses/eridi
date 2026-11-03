"""M8: projects that span storage, defined by `project` entries in the record."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document

from conftest import SampleLab
from eridi.core.index.rebuild import rebuild
from eridi.core.models import ProjectFolder
from eridi.core.record.schema import parse_entry
from eridi.core.service import Eridi, EridiError, UnknownProjectError

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    second = tmp_path / "Shared Drive"
    (second / "Imaging").mkdir(parents=True)
    (second / "Imaging" / "confocal_settings.md").write_text(
        "Confocal laser power 4% for the antibody validation stains.\n", encoding="utf-8"
    )
    (second / "Admin").mkdir()
    (second / "Admin" / "budget.md").write_text("Antibody budget for 2027.\n", encoding="utf-8")
    with Eridi() as app:
        app.init("A. Okafor", "a.okafor@example.edu", tmp_path / "record")
        app.add_root(sample_lab.root, "Lab Drive")
        app.add_root(second, "Shared Drive")
        app.scan()
        yield app


def root_id(app: Eridi, label: str) -> str:
    return next(r.id for r in app.list_roots() if r.label == label)


def test_project_spanning_two_roots_shows_files_from_both(
    lab: Eridi, sample_lab: SampleLab, tmp_path: Path
) -> None:
    project = lab.create_project(
        "Antibody validation",
        folders=[str(sample_lab.root / "Protocols"), str(tmp_path / "Shared Drive" / "Imaging")],
    )
    assert project.folders == (
        ProjectFolder(root_id(lab, "Lab Drive"), "Protocols"),
        ProjectFolder(root_id(lab, "Shared Drive"), "Imaging"),
    )
    paths = {f.path for f in lab.project_files("antibody")}
    assert PROTOCOL in paths
    assert "Imaging/confocal_settings.md" in paths
    assert "Admin/budget.md" not in paths
    assert "Notes/lab_meeting_2026-10-14.md" not in paths


def test_folder_references(lab: Eridi, sample_lab: SampleLab) -> None:
    lab_id = root_id(lab, "Lab Drive")
    p = lab.create_project("Whole drive", folders=["Lab Drive"])
    assert p.folders == (ProjectFolder(lab_id, ""),)
    p = lab.add_project_folder(p.id, f"{lab_id}:Notes")
    assert ProjectFolder(lab_id, "Notes") in p.folders
    p = lab.add_project_folder(p.id, f"{lab_id}:Notes")  # adding twice is harmless
    assert len(p.folders) == 2
    p = lab.remove_project_folder(p.id, f"{lab_id}:Notes")
    assert p.folders == (ProjectFolder(lab_id, ""),)
    with pytest.raises(EridiError, match="is a file"):
        lab.add_project_folder(p.id, str(sample_lab.protocol_docx))
    with pytest.raises(EridiError, match="isn't inside any connected storage"):
        lab.add_project_folder(p.id, "/nowhere/at/all")


def test_rename_via_new_entry_updates_views(lab: Eridi, sample_lab: SampleLab) -> None:
    p = lab.create_project("Antibody validaton", folders=[str(sample_lab.root / "Protocols")])
    renamed = lab.rename_project(p.id, "Antibody validation")
    assert renamed.id == p.id
    assert renamed.folders == p.folders  # renaming keeps the folders
    assert [x.name for x in lab.list_projects()] == ["Antibody validation"]
    assert lab.get_project("antibody val").id == p.id
    # Both entries are in the record; the newer one wins.
    entries = [e for e in lab.store.iter_entries() if e.kind == "project"]
    assert [e.project.name for e in entries if e.project] == [
        "Antibody validaton",
        "Antibody validation",
    ]


def test_project_survives_index_rebuild(lab: Eridi, sample_lab: SampleLab) -> None:
    p = lab.create_project("Antibody validation", folders=[str(sample_lab.root / "Protocols")])
    lab.rebuild_index()
    assert lab.list_projects() == [p]
    assert PROTOCOL in {f.path for f in lab.project_files(p.id)}


def test_names_and_lookup(lab: Eridi) -> None:
    lab.create_project("Antibody validation")
    lab.create_project("Antibody screening")
    with pytest.raises(EridiError, match="already a project"):
        lab.create_project("antibody  VALIDATION")
    with pytest.raises(EridiError, match="several projects"):
        lab.get_project("antibody")
    with pytest.raises(UnknownProjectError):
        lab.get_project("organoids")
    with pytest.raises(EridiError, match="needs a name"):
        lab.create_project("   ")


def test_home_storage(lab: Eridi) -> None:
    p = lab.create_project("Antibody validation")
    shared = root_id(lab, "Shared Drive")
    assert lab.set_project_home(p.id, shared).home_root_id == shared
    with pytest.raises(EridiError, match="no storage"):
        lab.set_project_home(p.id, "root_nope")


def test_search_scoped_to_project_excludes_others(
    lab: Eridi, sample_lab: SampleLab, tmp_path: Path
) -> None:
    validation = lab.create_project(
        "Antibody validation", folders=[str(tmp_path / "Shared Drive" / "Imaging")]
    )
    lab.create_project("Admin", folders=[str(tmp_path / "Shared Drive" / "Admin")])
    everywhere = {h.path for h in lab.search("antibody") if h.kind == "file"}
    assert {"Imaging/confocal_settings.md", "Admin/budget.md"} <= everywhere
    scoped = {
        h.path for h in lab.search("antibody", project="Antibody validation") if h.kind == "file"
    }
    assert scoped == {"Imaging/confocal_settings.md"}

    lab.add_note("Budget approved for new antibody lots.", project="Admin")
    in_project = lab.add_note("Antibody validation stains look clean.", project=validation.id)
    hits = lab.search("antibody", project=validation.id)
    entry_ids = {h.id for h in hits if h.kind == "entry"}
    assert in_project.id in entry_ids
    assert all("Budget" not in h.title for h in hits)


def test_notes_about_a_file_join_its_project(lab: Eridi, sample_lab: SampleLab) -> None:
    p = lab.create_project("Western blots", folders=[str(sample_lab.root / "Protocols")])
    lab.create_project("Everything", folders=["Lab Drive"])
    note = lab.add_note("Checked the dilution table.", file=PROTOCOL)
    assert note.project_id == p.id  # the most specific folder wins
    elsewhere = lab.add_note("Unrelated idea.")
    assert elsewhere.project_id is None
    explicit = lab.add_note("Filed elsewhere.", file=PROTOCOL, project="Everything")
    assert explicit.project_id != p.id
    assert [e.id for e in lab.recent(project=p.id) if e.kind == "note"] == [explicit.id, note.id]


def test_project_overview(lab: Eridi, sample_lab: SampleLab) -> None:
    p = lab.create_project("Western blots", folders=[str(sample_lab.root / "Protocols")])
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
    doc.save(str(sample_lab.protocol_docx))
    lab.scan()
    overview = lab.project_overview("western")
    assert overview.project == p
    assert [c.path for c in overview.pending] == [PROTOCOL]
    assert PROTOCOL in [f.path for f in overview.files]
    assert overview.entries[0].kind == "project"
    lab.add_note("Longer incubation for the new lot.", file=PROTOCOL)
    overview = lab.project_overview(p.id)
    assert overview.pending == []
    assert overview.entries[0].kind == "note"


def test_older_project_entries_without_folders_keep_folders(
    lab: Eridi, sample_lab: SampleLab
) -> None:
    """A `project` entry that doesn't list folders (written before M8) keeps the folders."""
    p = lab.create_project("Western blots", folders=[str(sample_lab.root / "Protocols")])
    latest = next(e for e in lab.store.iter_entries() if e.kind == "project")
    data = json.loads(next(lab.store.folder.rglob(f"{latest.id}.json")).read_text(encoding="utf-8"))
    del data["project"]["folders"]
    data["project"]["name"] = "Western blots (renamed)"
    data["id"] = "01" + "Z" * 24  # newer than every real ID
    entry = parse_entry(data)
    lab.store.append(entry)
    lab.scan()
    [current] = lab.list_projects()
    assert current.name == "Western blots (renamed)"
    assert current.folders == p.folders


def test_rebuild_matches_incremental_with_projects(lab: Eridi, sample_lab: SampleLab) -> None:
    lab.create_project("Western blots", folders=[str(sample_lab.root / "Protocols")])
    before = lab.conn.execute("SELECT * FROM project_roots ORDER BY 1, 2, 3").fetchall()
    roots = lab.list_roots()
    connectors = {r.id: lab.connector(r) for r in roots}
    rebuild(lab.conn, roots, lab.store, lab.snapshots, lab.config.settings, connectors)
    after = lab.conn.execute("SELECT * FROM project_roots ORDER BY 1, 2, 3").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
