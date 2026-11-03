"""M11.1: saving through the outbox: keep the bytes here first, upload, retry, confirm."""

import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.cli.main import app as cli
from eridi.core.outbox import (
    NameTakenError,
    Outbox,
    OutboxJob,
    SaveRefusedError,
    free_name,
    retry_delay,
)
from eridi.core.service import Eridi, EridiError


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


# --- the outbox on its own ---


def test_add_keeps_bytes_before_any_upload(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox")
    job = box.add("root_A", "Protocols/new.txt", b"hello")
    assert job.state == "saved" and job.waiting
    assert box.data(job) == b"hello"
    assert [j.id for j in box.jobs()] == [job.id]
    stored = json.loads((tmp_path / "outbox" / f"{job.id}.json").read_text("utf-8"))
    assert stored["path"] == "Protocols/new.txt" and "hello" not in json.dumps(stored)


def test_failed_upload_is_retried_with_backoff(tmp_path: Path) -> None:
    clock = Clock()
    box = Outbox(tmp_path / "outbox", clock=clock)
    job = box.add("root_A", "a.txt", b"x")
    calls: list[str] = []

    def offline(j: OutboxJob, data: bytes) -> str:
        calls.append(j.id)
        raise OSError("network is unreachable")

    [failed] = box.process(offline)
    assert failed.state == "queued" and failed.attempts == 1
    assert failed.next_attempt == clock.now + retry_delay(1)
    assert "unreachable" in (failed.last_error or "")
    assert box.process(offline) == []  # not due yet
    clock.now += retry_delay(1)
    [again] = box.process(offline)
    assert again.attempts == 2 and again.next_attempt == clock.now + retry_delay(2)
    assert len(calls) == 2

    clock.now += retry_delay(2)
    [done] = box.process(lambda j, d: j.path)
    assert done.state == "uploaded" and done.uploaded_path == "a.txt"
    assert not (tmp_path / "outbox" / f"{job.id}.bin").exists()  # local copy freed


def test_retry_delay_grows_to_an_hour() -> None:
    assert [retry_delay(n) for n in (1, 2, 3)] == [5, 10, 20]
    assert retry_delay(50) == 3600


def test_name_taken_and_refused_wait_for_the_user(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox")
    taken = box.add("root_A", "a.txt", b"x")

    def name_taken(j: OutboxJob, d: bytes) -> str:
        raise NameTakenError(j.path)

    def refused(j: OutboxJob, d: bytes) -> str:
        raise SaveRefusedError("Saving is off.")

    assert box.run(taken, name_taken).state == "needs_confirmation"
    refused_job = box.run(box.add("root_A", "b.txt", b"y"), refused)
    assert refused_job.state == "blocked" and refused_job.last_error == "Saving is off."
    assert box.due() == []  # neither is retried on its own
    retried = box.retry(taken.id, "rename")
    assert retried is not None and retried.on_exists == "rename" and retried.state == "queued"
    assert [j.id for j in box.due()] == [taken.id]


def test_uploaded_jobs_are_forgotten_after_30_days(tmp_path: Path) -> None:
    clock = Clock()
    box = Outbox(tmp_path / "outbox", clock=clock)
    box.process(lambda j, d: j.path)
    box.run(box.add("root_A", "a.txt", b"x"), lambda j, d: j.path)
    clock.now += 31 * 24 * 3600
    box.prune()
    assert box.jobs() == []


def test_free_name() -> None:
    taken = {"P/a.docx", "P/a (2).docx", "notes", "notes (2)"}
    assert free_name("P/a.docx", taken.__contains__) == "P/a (3).docx"
    assert free_name("notes", taken.__contains__) == "notes (3)"
    assert free_name(".env", set().__contains__) == ".env (2)"


# --- through the service, with a local folder ---


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", "a@example.edu", tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive", save=True)
        eridi.create_project(
            "Antibody validation", home_root_id=root.id, folders=[f"{root.id}:Protocols"]
        )
        eridi.scan()
        yield eridi


def test_save_into_a_project_lands_in_its_folder_and_is_indexed(
    lab: Eridi, sample_lab: SampleLab
) -> None:
    job = lab.save_file(b"Blocking: 5% milk, 1 h.\n", "blocking_v8.txt",
                        project="Antibody validation")  # fmt: skip
    assert job.state == "uploaded" and job.uploaded_path == "Protocols/blocking_v8.txt"
    assert (sample_lab.root / "Protocols" / "blocking_v8.txt").read_bytes().startswith(b"Block")
    [hit] = [h for h in lab.search("blocking milk") if h.path == "Protocols/blocking_v8.txt"]
    assert hit is not None
    assert [f.path for f in lab.project_files("Antibody validation")].count(
        "Protocols/blocking_v8.txt"
    ) == 1


def test_offline_save_then_reconnect_uploads(lab: Eridi, sample_lab: SampleLab) -> None:
    moved = sample_lab.root.with_name("unplugged")
    sample_lab.root.rename(moved)  # the drive is unplugged / the network share is gone
    job = lab.save_file(b"draft\n", "draft.txt", project="Antibody validation")
    assert job.state == "queued" and "can't be found" in (job.last_error or "")
    assert lab.outbox.data(job) == b"draft\n"  # safe on this computer

    moved.rename(sample_lab.root)  # back online
    lab.outbox.clock = lambda: job.next_attempt + 1
    [done] = lab.process_outbox()
    assert done.state == "uploaded"
    assert (sample_lab.root / "Protocols" / "draft.txt").read_bytes() == b"draft\n"


def test_crash_mid_upload_resumes(lab: Eridi, sample_lab: SampleLab, tmp_path: Path) -> None:
    job = lab.save_file(b"v1\n", "crash.txt", project="Antibody validation", upload=False)
    lab.outbox.update(job, state="uploading", attempts=1)  # Eridi died while uploading
    with Eridi() as restarted:
        [done] = restarted.process_outbox()
    assert done.state == "uploaded" and done.attempts == 2
    assert (sample_lab.root / "Protocols" / "crash.txt").read_bytes() == b"v1\n"


def test_crash_after_upload_does_not_ask_about_its_own_file(
    lab: Eridi, sample_lab: SampleLab
) -> None:
    job = lab.save_file(b"same\n", "twice.txt", project="Antibody validation", upload=False)
    (sample_lab.root / "Protocols" / "twice.txt").write_bytes(b"same\n")  # it got there
    lab.outbox.update(job, state="uploading")
    [done] = lab.process_outbox()
    assert done.state == "uploaded"


def test_save_to_storage_with_save_off_is_refused(
    lab: Eridi, sample_lab: SampleLab, tmp_path: Path
) -> None:
    other_folder = tmp_path / "Other"
    other_folder.mkdir()
    other = lab.add_root(other_folder, "Read only")  # Save is off by default
    with pytest.raises(EridiError, match="Saving to 'Read only' is off"):
        lab.save_file(b"x", "x.txt", storage=other.id)
    assert lab.outbox_jobs() == []
    assert list(other_folder.iterdir()) == []

    # Turned off after the file was queued: the upload is refused, not retried.
    job = lab.save_file(b"x", "late.txt", project="Antibody validation", upload=False)
    lab.update_root(job.root_id, save=False)
    [blocked] = lab.process_outbox()
    assert blocked.state == "blocked" and "off" in (blocked.last_error or "")
    assert not (sample_lab.root / "Protocols" / "late.txt").exists()


def test_project_without_home_storage_says_how_to_set_one(lab: Eridi) -> None:
    lab.create_project("Imaging")
    with pytest.raises(EridiError, match="no home storage"):
        lab.save_file(b"x", "x.txt", project="Imaging")


def test_cancel_deletes_only_the_local_copy(lab: Eridi, sample_lab: SampleLab) -> None:
    job = lab.save_file(b"x", "never.txt", project="Antibody validation", upload=False)
    lab.cancel_save(job.id)
    assert lab.outbox_jobs() == []
    assert not (sample_lab.root / "Protocols" / "never.txt").exists()


# --- API and CLI ---

PORT = 8765
AUTH = {"Authorization": "Bearer test-token-0123456789"}


def test_api_save_and_outbox(lab: Eridi, sample_lab: SampleLab) -> None:
    app = create_app(lab, token="test-token-0123456789", port=PORT,  # noqa: S106
                     scanner=BackgroundScanner(lab, 0))  # fmt: skip
    project = lab.get_project("Antibody validation")
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.post(
            "/api/save", params={"name": "api.txt", "project": project.id},
            content=b"from the browser\n",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
        )  # fmt: skip
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        deadline = time.monotonic() + 10  # the app's background thread uploads it
        while time.monotonic() < deadline:
            saves = client.get("/api/outbox", headers=AUTH).json()["saves"]
            [mine] = [s for s in saves if s["id"] == job_id]
            if mine["state"] == "uploaded":
                break
            time.sleep(0.05)
        assert mine["state"] == "uploaded" and mine["path"] == "Protocols/api.txt"
        assert mine["project_id"] == project.id and mine["file"].endswith(":Protocols/api.txt")
        bad = client.post("/api/save", params={"name": "x.txt"}, content=b"x", headers=AUTH)
        assert bad.status_code == 400 and "where to save" in bad.json()["error"]
    assert (sample_lab.root / "Protocols" / "api.txt").exists()


def test_cli_save_and_outbox(lab: Eridi, sample_lab: SampleLab, tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "result.csv"
    source.write_text("lane,signal\n1,0.8\n", encoding="utf-8")
    result = runner.invoke(cli, ["save", str(source), "--project", "Antibody validation"])
    assert result.exit_code == 0, result.output
    assert "uploaded" in result.output and "Protocols/result.csv" in result.output
    again = runner.invoke(cli, ["save", str(source), "--project", "Antibody validation"])
    assert "uploaded" in again.output  # identical bytes already there: nothing to ask
    source.write_text("lane,signal\n1,0.9\n", encoding="utf-8")
    taken = runner.invoke(cli, ["save", str(source), "--project", "Antibody validation"])
    assert "name taken" in taken.output and "--new-name" in taken.output
    listing = runner.invoke(cli, ["outbox"])
    assert listing.exit_code == 0 and listing.output.count("Protocols/result.csv") >= 2
    shutil.rmtree(lab.outbox.folder)
    assert "Nothing saved" in runner.invoke(cli, ["outbox"]).output
