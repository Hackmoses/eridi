"""M3.2: files and their text in the index."""

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from eridi.core.hashing import sha256_text
from eridi.core.index.db import open_index
from eridi.core.index.files import (
    file_text,
    get_file,
    list_files,
    list_roots,
    set_status,
    text_versions,
    upsert_file,
    upsert_root,
)
from eridi.core.models import FileInfo, Root


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_index(Path(":memory:"))
    yield c
    c.close()


def info(text: str, path: str = "Protocols/WB.docx", **kw: object) -> FileInfo:
    return FileInfo(
        root_id="root_A",
        path=path,
        size=len(text),
        modified=1.0,
        ext=".docx",
        content_sha256="c" + sha256_text(text)[1:],
        text_sha256=sha256_text(text),
        title="Western blot",
        extract_status="ok",
        **kw,  # type: ignore[arg-type]
    )


def fts_match(conn: sqlite3.Connection, query: str) -> list[int]:
    rows = conn.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (query,))
    return [r[0] for r in rows]


def test_roots(conn: sqlite3.Connection) -> None:
    upsert_root(conn, Root("root_A", "Lab Drive", "/lab"))
    upsert_root(conn, Root("root_A", "Lab OneDrive", "/lab", search=False))
    assert list_roots(conn) == [Root("root_A", "Lab OneDrive", "/lab", search=False)]


def test_insert_and_get(conn: sqlite3.Connection) -> None:
    stored = upsert_file(conn, info("Block in 5% BSA."), "Block in 5% BSA.")
    assert stored.id is not None
    got = get_file(conn, "root_A", "Protocols/WB.docx")
    assert got == stored
    assert got.last_seen is not None and got.last_seen.endswith("Z")
    assert file_text(conn, stored.id) == "Block in 5% BSA."
    assert fts_match(conn, "bsa") == [stored.id]
    assert fts_match(conn, "western") == [stored.id]  # title is searchable
    assert fts_match(conn, "protocols") == [stored.id]  # and so is the path
    assert get_file(conn, "root_A", "missing.docx") is None


def test_update_replaces_text_and_records_versions(conn: sqlite3.Connection) -> None:
    v1 = upsert_file(conn, info("Incubate 1 h at RT."), "Incubate 1 h at RT.")
    v2 = upsert_file(conn, info("Incubate 2 h at RT, overnight."), "Incubate 2 h at RT, overnight.")
    assert v1.id == v2.id
    assert v2.id is not None
    assert fts_match(conn, "overnight") == [v2.id]
    assert conn.execute("SELECT count(*) FROM files_fts").fetchone()[0] == 1
    versions = [sha for sha, _, source in text_versions(conn, v2.id)]
    assert versions == [
        sha256_text("Incubate 1 h at RT."),
        sha256_text("Incubate 2 h at RT, overnight."),
    ]
    assert {source for _, _, source in text_versions(conn, v2.id)} == {"scan"}
    # The same text again adds no new version.
    upsert_file(conn, info("Incubate 2 h at RT, overnight."), "Incubate 2 h at RT, overnight.")
    assert len(text_versions(conn, v2.id)) == 2


def test_none_text_keeps_indexed_text(conn: sqlite3.Connection) -> None:
    stored = upsert_file(conn, info("Keep this text."), "Keep this text.")
    upsert_file(conn, replace(info("Keep this text."), size=999), None)
    assert stored.id is not None
    assert file_text(conn, stored.id) == "Keep this text."
    assert get_file(conn, "root_A", stored.path).size == 999  # type: ignore[union-attr]


def test_status_and_listing(conn: sqlite3.Connection) -> None:
    upsert_file(conn, info("a", path="b.md"), "a")
    upsert_file(conn, info("b", path="a.md"), "b")
    upsert_file(conn, FileInfo("root_B", "z.md", 1, 1.0, ".md"), "z")
    set_status(conn, "root_A", "b.md", "missing")
    assert [f.path for f in list_files(conn, "root_A")] == ["a.md", "b.md"]
    assert [f.root_id for f in list_files(conn)] == ["root_A", "root_A", "root_B"]
    assert get_file(conn, "root_A", "b.md").status == "missing"  # type: ignore[union-attr]


def test_unicode_paths(conn: sqlite3.Connection) -> None:
    stored = upsert_file(
        conn, info("Färbeprotokoll", path="Protokolle_Ü/Färbung ñ v2.md"), "Färbeprotokoll"
    )
    assert get_file(conn, "root_A", "Protokolle_Ü/Färbung ñ v2.md") == stored
    assert fts_match(conn, "färbeprotokoll") == [stored.id]
    assert fts_match(conn, "farbeprotokoll") == [stored.id]  # diacritics folded
