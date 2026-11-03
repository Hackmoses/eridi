"""M3.3: loading the record into the index."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import entry
from eridi.core.index.db import get_meta, open_index
from eridi.core.index.records import LAST_ENTRY_KEY, RECONCILE_INTERVAL, sync_records
from eridi.core.models import EntryChange, ProjectRef, Subject
from eridi.core.record.store import RecordStore

ROOT = "root_A"
PROTOCOL = Subject(ROOT, "Protocols/WB_v7.docx", text_sha256="t1")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_index(Path(":memory:"))
    yield c
    c.close()


@pytest.fixture
def store(tmp_path: Path) -> RecordStore:
    return RecordStore.init(tmp_path / "record")


def search_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    rows = conn.execute(
        "SELECT e.id FROM entries_fts f JOIN entries e ON e.seq = f.rowid "
        "WHERE entries_fts MATCH ? ORDER BY e.id",
        (query,),
    )
    return [r[0] for r in rows]


def states(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, state FROM entries")}


def test_loads_entries_subjects_and_search(conn: sqlite3.Connection, store: RecordStore) -> None:
    note = entry(
        text="Switched the blocking buffer to BSA.",
        subjects=[PROTOCOL],
        tags=["blocking"],
        change=EntryChange("t0", "t1", "Blocking: milk → BSA"),
    )
    store.append(note)
    report = sync_records(conn, store)
    assert report.full
    assert report.added == 1
    assert search_ids(conn, "bsa") == [note.id]
    assert search_ids(conn, "tags:blocking") == [note.id]
    assert search_ids(conn, "milk") == [note.id]  # the change summary
    assert search_ids(conn, "wb_v7") == [note.id]  # the file's name
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (note.id,)).fetchone()
    assert row["author_name"] == "A. Okafor"
    assert row["change_to_text_sha256"] == "t1"
    subjects = conn.execute("SELECT root_id, path, text_sha256 FROM entry_subjects").fetchall()
    assert [tuple(s) for s in subjects] == [(ROOT, PROTOCOL.path, "t1")]
    assert get_meta(conn, LAST_ENTRY_KEY) == note.id


def test_incremental_reads_only_new_entries(
    conn: sqlite3.Connection, store: RecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = entry(text="first")
    store.append(first)
    sync_records(conn, store)
    second = entry(text="second")
    store.append(second)

    seen_after: list[str | None] = []
    real_read = store.read

    def spy(after: str | None = None) -> object:
        seen_after.append(after)
        return real_read(after)

    monkeypatch.setattr(store, "read", spy)
    report = sync_records(conn, store)
    assert not report.full
    assert seen_after == [first.id]
    assert report.added == 1
    assert search_ids(conn, "second") == [second.id]
    # Nothing new: nothing added.
    assert sync_records(conn, store).added == 0


def test_supersede_and_tombstone_update_search(
    conn: sqlite3.Connection, store: RecordStore
) -> None:
    a = entry(text="incubate one hour")
    store.append(a)
    sync_records(conn, store)
    b = entry(text="incubate two hours", supersedes=a.id)
    store.append(b)
    sync_records(conn, store)
    assert search_ids(conn, "incubate") == [b.id]
    assert states(conn) == {a.id: "superseded", b.id: "current"}

    t = entry("tombstone", "", target_id=b.id)
    store.append(t)
    sync_records(conn, store)
    assert search_ids(conn, "incubate") == []
    assert states(conn)[b.id] == "retracted"


def test_late_synced_old_entry_is_found_by_full_reconcile(
    conn: sqlite3.Connection, store: RecordStore, tmp_path: Path
) -> None:
    # An entry written earlier on another laptop arrives after newer local entries.
    other = RecordStore.init(tmp_path / "other laptop")
    late = entry(text="written offline on the other laptop")
    other.append(late)
    newer = entry(text="newer local")
    store.append(newer)
    now = datetime.now(UTC)
    sync_records(conn, store, now=now)

    target = store.path_for(late)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(other.path_for(late).read_bytes())

    assert sync_records(conn, store, now=now + timedelta(minutes=1)).added == 0  # incremental
    report = sync_records(conn, store, now=now + RECONCILE_INTERVAL)
    assert report.full
    assert report.added == 1
    assert search_ids(conn, "offline") == [late.id]


def test_full_reconcile_drops_entries_whose_files_are_gone(
    conn: sqlite3.Connection, store: RecordStore
) -> None:
    keep, lost = entry(text="keep"), entry(text="lost")
    store.append(keep)
    path = store.append(lost)
    sync_records(conn, store)
    path.unlink()
    report = sync_records(conn, store, full=True)
    assert report.removed == 1
    assert set(states(conn)) == {keep.id}
    assert search_ids(conn, "lost") == []
    assert conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0] == 1


def test_tombstone_arriving_before_its_target(
    conn: sqlite3.Connection, store: RecordStore, tmp_path: Path
) -> None:
    target = entry(text="retract me")
    tomb = entry("tombstone", "", target_id=target.id)
    store.append(tomb)
    sync_records(conn, store)
    store.append(target)
    sync_records(conn, store, full=True)
    assert states(conn)[target.id] == "retracted"
    assert search_ids(conn, "retract") == []


def test_projects_from_project_entries(conn: sqlite3.Connection, store: RecordStore) -> None:
    pid = "proj_01JC8X0000000000000000000A"
    store.append(entry("project", "", project=ProjectRef(pid, "Antibody validation")))
    store.append(
        entry("project", "", project=ProjectRef(pid, "Antibody validation 2026", "root_A"))
    )
    sync_records(conn, store)
    rows = conn.execute("SELECT id, name, home_root_id FROM projects").fetchall()
    assert [tuple(r) for r in rows] == [(pid, "Antibody validation 2026", "root_A")]


def test_problems_are_reported(conn: sqlite3.Connection, store: RecordStore) -> None:
    path = store.append(entry())
    path.with_name(path.stem + " (1).json").write_bytes(path.read_bytes())
    report = sync_records(conn, store)
    assert report.added == 1
    assert len(report.problems) == 1


def test_two_processes_loading_the_same_new_entry(tmp_path: Path) -> None:
    """The web app and `eridi watch` share one index; both may see a labmate's new entry
    before either has stored it. The second must skip it, not fail or duplicate it."""
    import eridi.core.index.records as records

    store = RecordStore.init(tmp_path / "record")
    index = tmp_path / "index.sqlite"
    app_conn, watch_conn = open_index(index), open_index(index)
    note = entry(text="Gel imager booked.", subjects=[Subject("root_A", "a.docx")])
    store.append(note)
    real = records._known_ids
    seen_before = real(watch_conn)  # the watcher looked before the app stored the entry
    sync_records(app_conn, store)
    records._known_ids = lambda conn: seen_before  # type: ignore[assignment]
    try:
        report = sync_records(watch_conn, store, full=True)  # reads every entry file
    finally:
        records._known_ids = real
    assert report.added == 0 and report.added_ids == []
    assert watch_conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    assert watch_conn.execute("SELECT count(*) FROM entry_subjects").fetchone()[0] == 1
