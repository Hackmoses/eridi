"""M1.3: two devices appending to the same record folder at once lose nothing."""

import json
import multiprocessing
from multiprocessing.queues import Queue
from pathlib import Path

from eridi.core.models import Author
from eridi.core.record.schema import make_entry
from eridi.core.record.store import RecordStore

PER_WRITER = 500


def _write_many(folder: str, device: str, count: int, seen: "Queue[str]") -> None:
    store = RecordStore.init(Path(folder))
    seen.put(store.id)
    author = Author(f"Writer {device}")
    for i in range(count):
        store.append(make_entry("note", author, device, f"{device} entry {i}"))


def test_two_writers_lose_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "shared record"
    # "spawn" behaves the same on macOS, Windows and Linux.
    ctx = multiprocessing.get_context("spawn")
    devices = ["dev_LAPTOPA", "dev_LAPTOPB"]
    seen: Queue[str] = ctx.Queue()
    workers = [
        ctx.Process(target=_write_many, args=(str(folder), d, PER_WRITER, seen)) for d in devices
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=120)
        assert w.exitcode == 0

    store = RecordStore(folder)
    # Both devices created the folder at once; neither replaced the other's marker.
    assert {seen.get(timeout=5), seen.get(timeout=5)} == {store.id}
    read = store.read()
    assert read.problems == []
    assert len(read.entries) == 2 * PER_WRITER
    assert len({e.id for e in read.entries}) == 2 * PER_WRITER
    for device in devices:
        mine = [e for e in read.entries if e.device_id == device]
        # Each writer's entries keep the order they were written in.
        assert [e.text for e in mine] == [f"{device} entry {i}" for i in range(PER_WRITER)]

    files = list((folder / "entries").rglob("*.json"))
    assert len(files) == 2 * PER_WRITER
    for f in files:
        assert json.loads(f.read_text(encoding="utf-8"))["id"] == f.stem
    assert not list((folder / "entries" / ".tmp").iterdir())
