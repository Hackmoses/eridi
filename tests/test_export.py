"""M13.3: a project's record as Markdown and CSV, compared with fixture files."""

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.core.export import CSV_COLUMNS, export_csv, export_markdown
from eridi.core.models import Project
from eridi.core.outcomes import Outcomes
from eridi.core.record.schema import parse_entry
from eridi.core.record.views import current
from eridi.core.service import Eridi
from test_api import AUTH, BASE, PORT, TOKEN

FIXTURES = Path(__file__).parent / "fixtures" / "export"
PROJECT = Project("proj_01JC8X0000000000000000000A", "Antibody validation",
                  labels=("patient-data",))  # fmt: skip
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def entries() -> list:  # type: ignore[type-arg]
    raw = json.loads((FIXTURES / "entries.json").read_text("utf-8"))
    return current(parse_entry(item) for item in raw)


def label(root_id: str) -> str | None:
    return "Lab OneDrive" if root_id.startswith("root_") else None


def test_markdown_matches_fixture() -> None:
    chosen = entries()
    text = export_markdown(PROJECT, chosen, Outcomes(chosen), label, now=NOW)
    assert text == (FIXTURES / "project.md").read_text("utf-8")


def test_csv_matches_fixture() -> None:
    chosen = entries()
    text = export_csv(chosen, Outcomes(chosen), label)
    assert text == (FIXTURES / "project.csv").read_bytes().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert tuple(rows[0]) == CSV_COLUMNS
    first = rows[0]
    assert (first["outcome"], first["result"]) == ("partial", "worked")  # updated later
    assert "\n" in first["text"]  # multi-line text survives, quoted
    assert all(r["text"] != "Wrong lane, ignore." for r in rows)  # retracted: left out


def test_empty_project() -> None:
    text = export_markdown(PROJECT, [], Outcomes([]), label, now=NOW)
    assert "Nothing recorded yet." in text and "0 entries" in text


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.create_project("Blots", folders=[f"{root.id}:Protocols"])
        eridi.scan()
        eridi.add_note("Plan: validate the new lot first.", project="Blots")
        eridi.add_note("Not about blots.")
        yield eridi


def test_cli_export(lab: Eridi, tmp_path: Path) -> None:
    runner = CliRunner()
    md = runner.invoke(cli, ["export", "Blots"])
    assert md.exit_code == 0, md.output
    assert md.output.startswith("# Blots\n") and "Plan: validate the new lot first." in md.output
    assert "Not about blots." not in md.output
    out = tmp_path / "blots.csv"
    saved = runner.invoke(cli, ["export", "Blots", "--format", "csv", "--out", str(out)])
    assert saved.exit_code == 0 and out.read_text("utf-8").startswith("id,created_at,kind")
    again = runner.invoke(cli, ["export", "Blots", "--out", str(out)])
    assert again.exit_code == 1 and "already exists" in again.output
    old = runner.invoke(cli, ["export", "Blots", "--month", "2020-01"])
    assert "0 entries" in old.output


def test_api_export(lab: Eridi) -> None:
    project = lab.get_project("Blots").id
    app = create_app(lab, token=TOKEN, port=PORT, scanner=BackgroundScanner(lab, 0))
    with TestClient(app, base_url=BASE) as client:
        r = client.get(f"/api/projects/{project}/export", params={"format": "csv"}, headers=AUTH)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == 'attachment; filename="Blots.csv"'
        md = client.get(f"/api/projects/{project}/export", headers=AUTH)
        assert md.headers["content-type"].startswith("text/markdown")
        bad = client.get(f"/api/projects/{project}/export", params={"format": "pdf"}, headers=AUTH)
        assert bad.status_code == 400
