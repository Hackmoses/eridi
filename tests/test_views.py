"""M13.2: what changed in a project this month, everything that failed, open questions."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import SampleLab, entry
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.core.changes import PendingChange
from eridi.core.models import EntryChange, format_timestamp
from eridi.core.outcomes import Outcomes
from eridi.core.reports import month_period, open_questions
from eridi.core.service import Eridi
from test_api import AUTH, BASE, PORT, TOKEN

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


def test_month_period_boundaries() -> None:
    december = month_period("2026-12")
    assert (december.since, december.until) == (
        "2026-12-01T00:00:00.000Z",
        "2027-01-01T00:00:00.000Z",
    )
    assert december.label == "December 2026"
    now = datetime(2026, 11, 3, 15, tzinfo=UTC)
    assert month_period(now=now).since == "2026-11-01T00:00:00.000Z"
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_period("November")


def test_open_questions_after_n_days() -> None:
    now = datetime(2026, 11, 30, tzinfo=UTC)
    old = entry("note", "Longer incubation.", change=EntryChange("a", "b", "1 h → 2 h"))
    old = type(old)(**{**old.__dict__, "created_at": "2026-11-01T10:00:00.000Z"})
    fresh = entry("note", "Today's change.", change=EntryChange("c", "d"))
    fresh = type(fresh)(**{**fresh.__dict__, "created_at": "2026-11-29T10:00:00.000Z"})
    answered = entry("decision", "Switch to BSA.")
    answered = type(answered)(**{**answered.__dict__, "created_at": "2026-11-02T10:00:00.000Z"})
    result = entry("outcome", "", outcome="worked", target_id=answered.id)
    plain = entry("note", "Just a thought.")  # no change, not a decision: not a question
    stale = PendingChange(1, "root_A", "p.docx", None, "x", "y", "2026-11-05T10:00:00.000Z", None)
    entries = [old, fresh, answered, result, plain]
    found = open_questions(entries, [stale], Outcomes(entries), days=14, now=now)
    assert [(q.kind, q.entry.id if q.entry else q.path) for q in found] == [
        ("no_outcome", old.id),
        ("no_why", "p.docx"),
    ]


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.create_project("Blots", folders=[f"{root.id}:Protocols"])
        eridi.scan()
        yield eridi


def edit(sample_lab: SampleLab, text: str) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = text
    doc.save(str(sample_lab.protocol_docx))


def test_this_month_in_a_project(lab: Eridi, sample_lab: SampleLab) -> None:
    edit(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    note = lab.add_note("New lot needs longer.", file=PROTOCOL)
    lab.add_outcome("worked", entry_id=note.id)
    lab.add_note("Unrelated plan.", project=None)  # not in the project
    report = lab.project_period("Blots")
    assert report.period == month_period()
    assert [(a.kind, a.path) for a in report.changes] == [("edited", PROTOCOL)]
    assert report.changes[0].explanations[0].entry_id == note.id
    assert report.results[note.id] == "worked"
    assert [e.text for e in report.entries if e.kind == "note"] == ["New lot needs longer."]
    assert report.counts["edited"] == 1 and report.counts["worked"] == 1
    assert report.counts["needs_why"] == 0
    assert lab.project_period("Blots", month_period("2020-01")).changes == []


def test_everything_that_failed_with_this_protocol(lab: Eridi, sample_lab: SampleLab) -> None:
    edit(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    tried = lab.add_note("Tried 2 h.", file=PROTOCOL)
    lab.add_outcome("failed", entry_id=tried.id, text="Still weak bands.")
    old_version = lab.file_history(PROTOCOL).versions[0].text_sha256
    version_fail = lab.add_outcome("failed", file=PROTOCOL, version=old_version)
    worked = lab.add_note("Milk works for pERK.", file=PROTOCOL, outcome="worked")
    # A rename keeps the history: failures follow the file.
    renamed = sample_lab.protocol_docx.with_name("Western_blot_blocking_v8.docx")
    sample_lab.protocol_docx.rename(renamed)
    lab.scan()
    found = lab.failures(file="Western_blot_blocking_v8.docx")
    assert [f.entry.id for f in found] == [version_fail.id, tried.id]
    assert found[1].verdict.entry.text == "Still weak bands."
    assert worked.id not in {f.entry.id for f in found}
    assert {f.entry.id for f in lab.failures(project="Blots")} == {version_fail.id, tried.id}


def test_open_questions_in_the_service(lab: Eridi, sample_lab: SampleLab) -> None:
    edit(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    assert lab.open_questions(project="Blots") == []  # too new for the 14-day default
    [q] = lab.open_questions(project="Blots", days=0)
    assert q.kind == "no_why" and q.path == PROTOCOL
    note = lab.add_note("New lot needs longer.", file=PROTOCOL)
    [q] = lab.open_questions(days=0)
    assert q.kind == "no_outcome" and q.entry is not None and q.entry.id == note.id
    lab.add_outcome("partial", entry_id=note.id)
    assert lab.open_questions(days=0) == []


def test_cli_views(lab: Eridi, sample_lab: SampleLab) -> None:
    edit(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    note = lab.add_note("New lot needs longer.", file=PROTOCOL)
    runner = CliRunner()
    month = runner.invoke(cli, ["month", "Blots"])
    assert month.exit_code == 0, month.output
    assert f"Edited {PROTOCOL}" in month.output and "1 edited" in month.output
    questions = runner.invoke(cli, ["questions", "--days", "0"]).output
    assert "did it work?" in questions and note.id in questions
    lab.add_outcome("failed", entry_id=note.id, text="Bands still weak.")
    failed = runner.invoke(cli, ["failed", PROTOCOL]).output
    assert "New lot needs longer." in failed and "Bands still weak." in failed
    assert "isn't a month" in runner.invoke(cli, ["month", "Blots", "--month", "May"]).output


def test_api_views(lab: Eridi, sample_lab: SampleLab) -> None:
    edit(sample_lab, "Primary antibody incubation: 2 h at RT.")
    lab.scan()
    note = lab.add_note("New lot needs longer.", file=PROTOCOL)
    lab.add_outcome("failed", entry_id=note.id)
    project = lab.get_project("Blots").id
    app = create_app(lab, token=TOKEN, port=PORT, scanner=BackgroundScanner(lab, 0))
    with TestClient(app, base_url=BASE) as client:
        month = client.get("/api/views/month", params={"project": project}, headers=AUTH).json()
        assert month["counts"]["failed"] == 1
        assert month["changes"][0]["explanations"][0]["result"] == "failed"
        failed = client.get("/api/views/failed", params={"project": project}, headers=AUTH).json()
        assert failed["failures"][0]["entry"]["id"] == note.id
        q = client.get("/api/views/questions", params={"days": 0}, headers=AUTH).json()
        assert q["questions"] == [] and q["days"] == 0
        bad = client.get("/api/views/month", params={"project": project, "month": "x"},
                         headers=AUTH)  # fmt: skip
        assert bad.status_code == 400
    assert format_timestamp(datetime.now(UTC)) >= month["period"]["since"]
