"""M9.2: watching the shared record folder, and falling back to polling."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document

from conftest import TEST_DEVICE, SampleLab
from eridi.core.models import Author, Entry
from eridi.core.record.schema import make_entry
from eridi.core.record.store import RecordStore
from eridi.core.service import Eridi
from eridi.core.watch import RECORD, Watcher

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
LEE = Author("B. Lee", "human", None)


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        yield eridi


def wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def labmate_writes(lab: Eridi, text: str) -> Entry:
    """Another member's computer appends an entry; their sync app delivers the file."""
    entry = make_entry("note", LEE, TEST_DEVICE, text)
    RecordStore(lab.store.folder).append(entry)
    return entry


def test_watcher_picks_up_a_new_entry_within_the_debounce_window(lab: Eridi) -> None:
    arrived: list[Entry] = []
    debounce = 0.5
    watcher = Watcher(lab, debounce=debounce, on_entries=arrived.extend)
    watcher.start()
    try:
        assert RECORD in watcher.watching
        lab.recent()  # the app has already read the record once
        time.sleep(0.3)
        started = time.monotonic()
        entry = labmate_writes(lab, "Ordered a new lot of anti-pERK.")
        assert wait_for(lambda: bool(arrived)), "the new entry was not noticed"
        elapsed = time.monotonic() - started
        assert [e.id for e in arrived] == [entry.id]
        # The debounce window, plus the watcher's 0.5 s tick and the OS's event delivery
        # (macOS FSEvents can take a second or more).
        assert elapsed < debounce + 4.0
        assert lab.last_activity is not None
        assert [e.id for e in lab.recent()] == [entry.id]
        assert entry.id in {h.id for h in lab.search("anti-pERK lot")}
    finally:
        watcher.stop()


def test_own_entries_are_not_reported_as_arrivals(lab: Eridi) -> None:
    arrived: list[Entry] = []
    watcher = Watcher(lab, debounce=0.2, on_entries=arrived.extend)
    watcher.start()
    try:
        time.sleep(0.3)
        lab.add_note("My own note.")
        time.sleep(1.0)
        watcher.flush()
        assert arrived == []
    finally:
        watcher.stop()


def test_unwatchable_folders_are_polled_instead(
    lab: Eridi, sample_lab: SampleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "inotify watch limit reached")

    reports: list[str] = []
    arrived: list[Entry] = []
    watcher = Watcher(
        lab, debounce=0.1, poll_seconds=0.5, on_report=lambda root, r: reports.extend(r.changed),
        on_entries=arrived.extend,
    )  # fmt: skip
    monkeypatch.setattr(watcher._observer, "schedule", refuse)
    watcher.start()
    try:
        assert watcher.watching == []
        assert set(watcher.polled) == {lab.list_roots()[0].id, RECORD}
        assert "watch limit" in watcher.polled[RECORD]
        doc = Document(str(sample_lab.protocol_docx))
        doc.paragraphs[2].text = "Primary antibody incubation: 3 h at RT."
        doc.save(str(sample_lab.protocol_docx))
        entry = labmate_writes(lab, "Polling still finds this.")
        assert wait_for(lambda: PROTOCOL in reports and bool(arrived)), "polling found nothing"
        assert [e.id for e in arrived] == [entry.id]
    finally:
        watcher.stop()


def test_observer_that_cannot_start_falls_back_to_polling(
    lab: Eridi, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = Watcher(lab, poll_seconds=0.2)

    def broken() -> None:
        raise OSError(24, "too many open files")

    monkeypatch.setattr(watcher._observer, "start", broken)
    watcher.start()
    try:
        assert watcher.watching == []
        assert RECORD in watcher.polled and "too many open files" in watcher.polled[RECORD]
    finally:
        watcher.stop()


def test_watch_command_shows_labmates_notes(lab: Eridi, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import eridi.cli.main as cli_main

    real_sleep = time.sleep
    calls = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            real_sleep(seconds)  # `time.sleep` is patched for every thread
            return
        calls["n"] += 1
        if calls["n"] == 1:
            real_sleep(0.3)
            labmate_writes(lab, "Gel imager booked for Friday.")
            return
        real_sleep(3.5)  # past the 2 s debounce
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main.time, "sleep", fake_sleep)
    result = CliRunner().invoke(cli_main.app, ["watch"])
    assert "and the record" in result.output
    assert "B. Lee: Gel imager booked for Friday." in result.output
