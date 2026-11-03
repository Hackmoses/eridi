"""M7.4: background scans while the app is open."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import SampleLab
from eridi.api.scanner import BackgroundScanner
from eridi.core.service import Eridi


@pytest.fixture
def eridi(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as e:
        e.init("A", None, tmp_path / "record")
        e.add_root(sample_lab.root, "Lab Drive")
        yield e


def wait_for(condition: object, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_scans_on_start_and_then_on_a_timer(eridi: Eridi) -> None:
    scanner = BackgroundScanner(eridi, interval_minutes=0.5 / 60)  # every half second
    scanner.start()
    try:
        wait_for(lambda: scanner.scans >= 3)
    finally:
        scanner.stop()
    status = scanner.status
    assert status.last_finished is not None and not status.running
    assert status.last_summary is not None
    assert status.last_summary["storage"][0]["label"] == "Lab Drive"


def test_interval_off_means_only_on_request(eridi: Eridi) -> None:
    scanner = BackgroundScanner(eridi, interval_minutes=0)
    scanner.start()
    try:
        time.sleep(0.3)
        assert scanner.scans == 0
        assert scanner.request()
        wait_for(lambda: scanner.scans == 1)
        assert scanner.status.last_summary is not None
        assert scanner.status.last_summary["storage"][0]["new"] == 9
    finally:
        scanner.stop()


def test_errors_are_reported_not_raised(eridi: Eridi, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(eridi, "scan", broken)
    status = BackgroundScanner(eridi, 0).run_now()
    assert status.last_error == "disk vanished" and not status.running


def test_interval_comes_from_settings(eridi: Eridi) -> None:
    from eridi.api.app import create_app

    app = create_app(eridi, token="t", port=1)  # noqa: S106 - test value
    assert app.state.scanner.interval == eridi.config.settings.scan_interval_minutes * 60


def test_status_answers_during_a_scan(eridi: Eridi, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from eridi.api.app import create_app

    scanner = BackgroundScanner(eridi, 0)
    app = create_app(eridi, token="t", port=1, scanner=scanner)  # noqa: S106 - test value
    scanner.status.running = True
    with TestClient(app, base_url="http://127.0.0.1:1") as client:
        status = client.get("/api/status", headers={"Authorization": "Bearer t"}).json()
    assert status["pending_count"] is None and status["scan"]["running"]
