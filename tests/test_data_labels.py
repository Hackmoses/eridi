"""M11.3: data-safety labels on projects; saving only to storage approved for them."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.core.config import load_config
from eridi.core.service import Eridi, EridiError
from test_api import AUTH, BASE, PORT
from test_api import TOKEN as API_TOKEN


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive", save=True)
        eridi.create_project("Clinical study", home_root_id=root.id)
        yield eridi


def test_labeled_project_refuses_an_unapproved_root(lab: Eridi, sample_lab: SampleLab) -> None:
    project = lab.set_project_labels("Clinical study", ["patient-data"])
    assert project.labels == ("patient-data",)
    with pytest.raises(EridiError, match="contains patient data, and 'Lab Drive' isn't approved"):
        lab.save_file(b"MRN,age\n", "cohort.csv", project="Clinical study")
    assert lab.outbox_jobs() == []
    assert not (sample_lab.root / "cohort.csv").exists()

    root = lab.update_root(project.home_root_id or "", approved_labels=["patient-data"])
    assert root.approved_labels == ("patient-data",)
    assert load_config().roots[0].approved_labels == ("patient-data",)  # kept in config.toml
    job = lab.save_file(b"MRN,age\n", "cohort.csv", project="Clinical study")
    assert job.state == "uploaded"


def test_label_added_after_queueing_blocks_the_upload(lab: Eridi, sample_lab: SampleLab) -> None:
    job = lab.save_file(b"x", "later.csv", project="Clinical study", upload=False)
    lab.set_project_labels("Clinical study", ["patient-data"])
    [blocked] = lab.process_outbox()
    assert blocked.id == job.id and blocked.state == "blocked"
    assert "patient data" in (blocked.last_error or "")
    assert not (sample_lab.root / "later.csv").exists()


def test_unlabeled_projects_and_direct_saves_are_unaffected(lab: Eridi) -> None:
    other = lab.create_project("Methods", home_root_id=lab.list_roots()[0].id)
    assert other.labels == ()
    assert lab.save_file(b"x", "m.txt", project="Methods").state == "uploaded"


def test_labels_live_in_the_record_and_survive_renames_and_rebuilds(lab: Eridi) -> None:
    lab.set_project_labels("Clinical study", ["patient-data"])
    lab.rename_project("Clinical study", "Clinical study 2026")
    assert lab.get_project("Clinical study 2026").labels == ("patient-data",)
    lab.rebuild_index()
    assert lab.get_project("Clinical study 2026").labels == ("patient-data",)
    lab.set_project_labels("Clinical study 2026", [])
    assert lab.get_project("Clinical study 2026").labels == ()


def test_bad_label_is_explained(lab: Eridi) -> None:
    with pytest.raises(EridiError, match="isn't a data label"):
        lab.set_project_labels("Clinical study", ["Patient data!"])


def test_cli_label_and_approve(lab: Eridi) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["project", "label", "Clinical study", "patient-data"])
    assert result.exit_code == 0 and "Contains patient data" in result.output
    approved = runner.invoke(cli, ["storage", "set", "Lab Drive", "--approve", "patient-data"])
    assert approved.exit_code == 0 and "approved for patient data" in approved.output
    listing = runner.invoke(cli, ["storage", "list"]).output
    assert "approved for patient data" in listing
    withdrawn = runner.invoke(cli, ["storage", "set", "Lab Drive", "--unapprove", "patient-data"])
    assert "approved for" not in withdrawn.output
    removed = runner.invoke(cli, ["project", "label", "Clinical study", "patient-data", "--remove"])
    assert "patient data" not in removed.output


def test_api_labels_and_approval(lab: Eridi) -> None:
    app = create_app(lab, token=API_TOKEN, port=PORT, scanner=BackgroundScanner(lab, 0))
    project = lab.get_project("Clinical study")
    root_id = lab.list_roots()[0].id
    with TestClient(app, base_url=BASE) as client:
        r = client.patch(f"/api/projects/{project.id}", json={"labels": ["patient-data"]},
                         headers=AUTH)  # fmt: skip
        assert r.status_code == 200 and r.json()["labels"] == ["patient-data"]
        refused = client.post("/api/save", params={"name": "a.csv", "project": project.id},
                              content=b"x", headers=AUTH)  # fmt: skip
        assert refused.status_code == 400 and "patient data" in refused.json()["error"]
        s = client.patch(f"/api/storage/{root_id}", json={"approved_labels": ["patient-data"]},
                         headers=AUTH)  # fmt: skip
        assert s.json()["approved_labels"] == ["patient-data"]
        ok = client.post("/api/save", params={"name": "a.csv", "project": project.id},
                         content=b"x", headers=AUTH)  # fmt: skip
        assert ok.status_code == 202
