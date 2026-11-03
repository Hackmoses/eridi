"""M13.1: outcomes for an entry or a file version, and quick outcome buttons' API."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import SampleLab, entry
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.core.models import EntryChange, Subject
from eridi.core.outcomes import Outcomes
from eridi.core.service import Eridi, EridiError
from test_api import AUTH, BASE, PORT, TOKEN

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


def test_newest_outcome_wins_and_reaches_the_version() -> None:
    note = entry("note", "Longer incubation.", outcome="partial",
                 change=EntryChange("aaa", "bbb", "1 h → 2 h"))  # fmt: skip
    assert Outcomes([note]).outcome_of(note) == "partial"
    assert Outcomes([note]).for_version("bbb").outcome == "partial"  # type: ignore[union-attr]

    later = entry("outcome", "", outcome="worked", target_id=note.id)
    outcomes = Outcomes([note, later])
    assert outcomes.outcome_of(note) == "worked"

    on_version = entry("outcome", "", outcome="failed",
                       subjects=[Subject("root_A", "p.docx", text_sha256="bbb")])  # fmt: skip
    outcomes = Outcomes([note, later, on_version])
    assert outcomes.outcome_of(note) == "failed"  # the newest says it failed after all
    assert outcomes.for_version("bbb").entry.id == on_version.id  # type: ignore[union-attr]
    assert Outcomes([]).outcome_of(entry("note", "No result yet.")) is None


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        doc = Document(str(sample_lab.protocol_docx))
        doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
        doc.save(str(sample_lab.protocol_docx))
        eridi.scan()
        yield eridi


def test_outcome_for_an_earlier_version(lab: Eridi) -> None:
    history = lab.file_history(PROTOCOL)
    old, new = history.versions
    lab.add_outcome("failed", file=PROTOCOL, version=old.text_sha256[:8])
    lab.add_outcome("worked", file=PROTOCOL)  # the current version
    versions = {v.text_sha256: v.outcome for v in lab.file_history(PROTOCOL).versions}
    assert versions == {old.text_sha256: "failed", new.text_sha256: "worked"}
    with pytest.raises(EridiError, match="no single version"):
        lab.add_outcome("worked", file=PROTOCOL, version="ffffffff")
    with pytest.raises(EridiError, match="give the file"):
        lab.add_outcome("worked", version=old.text_sha256)


def test_an_outcome_is_not_a_why(lab: Eridi) -> None:
    assert len(lab.pending_changes()) == 1
    lab.add_outcome("worked", file=PROTOCOL)
    assert len(lab.pending_changes()) == 1  # still needs someone to say why it changed
    lab.add_note("New antibody lot needs longer.", file=PROTOCOL)
    assert lab.pending_changes() == []


def test_outcome_on_a_note_shows_in_history(lab: Eridi) -> None:
    note = lab.add_note("New antibody lot needs longer.", file=PROTOCOL)
    lab.add_outcome("worked", entry_id=note.id)
    outcomes = lab.outcomes()
    assert outcomes.outcome_of(note) == "worked"
    current = [v for v in lab.file_history(PROTOCOL).versions if v.is_current]
    assert current[0].outcome == "worked"  # the version it explains

    retraction = lab.add_outcome("failed", entry_id=note.id)
    lab.retract(retraction.id, "Wrong blot.")
    assert lab.outcomes().outcome_of(note) == "worked"  # a retracted outcome doesn't count


def test_cli_outcome_for_a_version(lab: Eridi) -> None:
    old = lab.file_history(PROTOCOL).versions[0].text_sha256
    runner = CliRunner()
    result = runner.invoke(cli, ["outcome", PROTOCOL, "failed", "--version", old[:8]])
    assert result.exit_code == 0, result.output
    history = runner.invoke(cli, ["history", PROTOCOL]).output
    assert f"{old[:8]}  failed" in history


def test_api_quick_outcomes(lab: Eridi) -> None:
    app = create_app(lab, token=TOKEN, port=PORT, scanner=BackgroundScanner(lab, 0))
    note = lab.add_note("New antibody lot needs longer.", file=PROTOCOL, outcome="partial")
    file_ref = f"{lab.list_roots()[0].id}:{PROTOCOL}"
    with TestClient(app, base_url=BASE) as client:
        r = client.post("/api/outcomes", json={"entry_id": note.id, "outcome": "worked"},
                        headers=AUTH)  # fmt: skip
        assert r.status_code == 201
        old = lab.file_history(PROTOCOL).versions[0].text_sha256
        r = client.post("/api/outcomes", headers=AUTH,
                        json={"file": file_ref, "version": old, "outcome": "failed"})  # fmt: skip
        assert r.status_code == 201
        h = client.get("/api/history", params={"file": file_ref}, headers=AUTH).json()
        assert [v["outcome"] for v in h["versions"]] == ["failed", "worked"]
        [mine] = [e for e in h["entries"] if e["id"] == note.id]
        assert mine["outcome"] == "partial" and mine["result"] == "worked"
