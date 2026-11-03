"""M4.1: snapshots and the version log."""

import gzip
import os
from pathlib import Path

import pytest

from eridi.core.hashing import sha256_text
from eridi.core.snapshots import SnapshotStore, VersionLine, replay

T1 = "2026-11-03T10:00:00.000Z"
T2 = "2026-11-04T10:00:00.000Z"
T3 = "2026-11-05T10:00:00.000Z"


@pytest.fixture
def snaps(isolated_home: Path) -> SnapshotStore:
    return SnapshotStore()


def test_put_get_and_location(snaps: SnapshotStore, isolated_home: Path) -> None:
    sha = snaps.put("Primary antibody incubation: 1 h at RT.")
    assert sha == sha256_text("Primary antibody incubation: 1 h at RT.")
    path = isolated_home / "snapshots" / sha[:2] / f"{sha}.txt.gz"
    assert path.is_file()
    assert gzip.decompress(path.read_bytes()).decode() == "Primary antibody incubation: 1 h at RT."
    assert snaps.get(sha) == "Primary antibody incubation: 1 h at RT."
    assert snaps.has(sha)
    assert snaps.get("0" * 64) is None
    assert snaps.get("not-a-sha") is None


def test_identical_text_is_stored_once(snaps: SnapshotStore) -> None:
    a = snaps.put("same text, Färbung")
    b = snaps.put("same text, Färbung")
    assert a == b
    assert len(list(snaps.folder.glob("??/*.txt.gz"))) == 1


def _aged(snaps: SnapshotStore, text: str, age: int) -> str:
    sha = snaps.put(text * 200)
    t = 1_700_000_000 + age
    os.utime(snaps.path_for(sha), (t, t))
    return sha


def test_eviction_removes_oldest_first(snaps: SnapshotStore) -> None:
    oldest = _aged(snaps, "a", 1)
    middle = _aged(snaps, "b", 2)
    newest = _aged(snaps, "c", 3)
    size = snaps.path_for(oldest).stat().st_size
    snaps.cap_bytes = size * 2
    assert snaps.evict(referenced=[]) == [oldest]
    assert not snaps.has(oldest)
    assert snaps.has(middle) and snaps.has(newest)


def test_referenced_snapshots_survive_eviction(snaps: SnapshotStore) -> None:
    oldest = _aged(snaps, "a", 1)
    middle = _aged(snaps, "b", 2)
    newest = _aged(snaps, "c", 3)
    snaps.cap_bytes = 0
    removed = snaps.evict(referenced={oldest, newest})
    assert removed == [middle]
    assert snaps.has(oldest) and snaps.has(newest)


def test_version_log_round_trip_and_dedup(snaps: SnapshotStore) -> None:
    assert snaps.log_version("root_A", "Protokolle_Ü/a.md", "a" * 64, T1)
    assert not snaps.log_version("root_A", "Protokolle_Ü/a.md", "a" * 64, T2)  # already known
    snaps.log_move("root_A", "Protokolle_Ü/a.md", "b.md", T2)
    assert not snaps.log_path.exists()  # buffered until flushed (one fsync per batch)
    snaps.flush()
    lines = SnapshotStore(snaps.folder).read_log()
    assert lines == [
        VersionLine("version", "root_A", "Protokolle_Ü/a.md", T1, "a" * 64, "scan"),
        VersionLine("move", "root_A", "b.md", T2, from_path="Protokolle_Ü/a.md"),
    ]
    assert "Protokolle_Ü" in snaps.log_path.read_text(encoding="utf-8")


def test_torn_last_line_is_ignored(snaps: SnapshotStore) -> None:
    snaps.log_version("root_A", "a.md", "a" * 64, T1)
    snaps.flush()
    with snaps.log_path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "version", "root_id": "root_A", "pa')  # crash mid-write
    assert [ln.text_sha256 for ln in snaps.read_log()] == ["a" * 64]
    # The next write starts on its own line, so it isn't glued to the torn one.
    fresh = SnapshotStore(snaps.folder)
    fresh.log_version("root_A", "a.md", "b" * 64, T2)
    assert [ln.text_sha256 for ln in fresh.read_log()] == ["a" * 64, "b" * 64]


def test_garbage_lines_are_ignored(snaps: SnapshotStore) -> None:
    snaps.folder.mkdir(parents=True, exist_ok=True)
    snaps.log_path.write_text(
        'not json\n{"kind": "version"}\n{"kind": "version", "root_id": "r", "path": "p", '
        f'"at": "{T1}", "text_sha256": "XYZ"}}\n\n',
        encoding="utf-8",
    )
    assert snaps.read_log() == []


def test_replay_follows_moves_and_keeps_earliest() -> None:
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    lines = [
        VersionLine("version", "r", "old.docx", T1, a),
        VersionLine("version", "r", "old.docx", T2, b),
        VersionLine("version", "r", "new.docx", T3, b),  # seen again after the rename
        VersionLine("move", "r", "new.docx", T3, from_path="old.docx"),
        VersionLine("version", "r", "new.docx", T3, c),
    ]
    versions, moves = replay(lines)
    assert set(versions) == {("r", "new.docx")}
    got = {sha: v.first_seen for sha, v in versions[("r", "new.docx")].items()}
    assert got == {a: T1, b: T2, c: T3}
    assert len(moves) == 1
