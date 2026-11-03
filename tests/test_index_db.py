"""M3.1: the index database."""

import sqlite3
from pathlib import Path

import pytest

import eridi.core.index.db as db
from eridi.core.index.db import (
    SCHEMA_VERSION,
    IndexUnavailableError,
    check_fts5,
    get_meta,
    index_path,
    open_index,
    set_meta,
)

TABLES = {
    "meta",
    "roots",
    "files",
    "text_versions",
    "files_fts",
    "entries",
    "entry_subjects",
    "entries_fts",
    "projects",
    "project_roots",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_fresh_creation(isolated_home: Path) -> None:
    conn = open_index()
    assert index_path() == isolated_home / "index.sqlite"
    assert index_path().is_file()
    assert tables(conn) >= TABLES
    assert get_meta(conn, "schema_version") == str(SCHEMA_VERSION)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_reopen_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    conn = open_index(path)
    with conn:
        set_meta(conn, "last_full_scan", "2026-11-03T10:00:00.000Z")
    conn.close()
    conn = open_index(path)
    assert get_meta(conn, "last_full_scan") == "2026-11-03T10:00:00.000Z"
    conn.close()


def test_version_mismatch_rebuilds(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    conn = open_index(path)
    with conn:
        set_meta(conn, "schema_version", "0")
        set_meta(conn, "last_full_scan", "old")
        conn.execute("CREATE TABLE leftover_from_old_version (x)")
        conn.execute(
            "INSERT INTO files_fts (rowid, path, title, body) VALUES (1, 'a', 'b', 'stale')"
        )
    conn.close()
    conn = open_index(path)
    assert get_meta(conn, "schema_version") == str(SCHEMA_VERSION)
    assert get_meta(conn, "last_full_scan") is None
    assert "leftover_from_old_version" not in tables(conn)
    assert conn.execute("SELECT count(*) FROM files_fts").fetchone()[0] == 0
    conn.close()


def test_damaged_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    path.write_bytes(b"this is not a database at all" * 100)
    conn = open_index(path)
    assert tables(conn) >= TABLES
    conn.close()


def test_in_memory() -> None:
    conn = open_index(Path(":memory:"))
    assert tables(conn) >= TABLES


def test_fts5_available() -> None:
    check_fts5()  # CI's Pythons all ship FTS5


def test_missing_fts5_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect

    class NoFts:
        def __init__(self) -> None:
            self.conn = real_connect(":memory:")

        def execute(self, sql: str) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self) -> None:
            self.conn.close()

    monkeypatch.setattr(db.sqlite3, "connect", lambda *_a, **_k: NoFts())
    with pytest.raises(IndexUnavailableError, match="FTS5"):
        check_fts5()
    with pytest.raises(IndexUnavailableError, match="uv python install"):
        open_index(Path(":memory:"))
