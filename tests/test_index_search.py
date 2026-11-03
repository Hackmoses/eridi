"""M3.4: unified search over files and the record."""

import random
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import SampleLab, entry
from eridi.connectors.local import LocalConnector
from eridi.core.index.db import open_index
from eridi.core.index.files import upsert_file, upsert_root
from eridi.core.index.ingest import index_root
from eridi.core.index.records import sync_records
from eridi.core.index.search import (
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    build_match,
    parse_query,
    search,
)
from eridi.core.models import FileInfo, Root, Subject
from eridi.core.record.store import RecordStore

ROOT = Root("root_LAB", "Lab Drive", "")
PROTOCOL_PATH = "Protocols/Western_blot_blocking_v7.docx"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = open_index(Path(":memory:"))
    yield c
    c.close()


@pytest.fixture
def lab_index(
    conn: sqlite3.Connection, sample_lab: SampleLab, tmp_path: Path
) -> tuple[sqlite3.Connection, RecordStore]:
    root = Root(ROOT.id, ROOT.label, str(sample_lab.root))
    index_root(conn, root, LocalConnector(sample_lab.root, root.id), size_limit=25 * 2**20)
    store = RecordStore.init(tmp_path / "record")
    store.append(
        entry(
            text="Changed the blocking buffer to BSA because milk gave high background.",
            subjects=[Subject(root.id, PROTOCOL_PATH, root_label=root.label)],
        )
    )
    store.append(entry(text="Ordered more GAPDH antibody."))
    sync_records(conn, store)
    return conn, store


def test_blocking_buffer_finds_protocol_and_note(
    lab_index: tuple[sqlite3.Connection, RecordStore],
) -> None:
    conn, _ = lab_index
    hits = search(conn, "blocking buffer")
    kinds_paths = [(h.kind, h.path) for h in hits]
    assert ("file", PROTOCOL_PATH) in kinds_paths
    assert ("entry", PROTOCOL_PATH) in kinds_paths
    note = next(h for h in hits if h.kind == "entry")
    assert note.root_label == "Lab Drive"
    assert note.entry_kind == "note"
    assert note.author == "A. Okafor"
    assert note.title.startswith("Changed the blocking buffer")
    assert f"{HIGHLIGHT_START}blocking{HIGHLIGHT_END}" in note.snippet
    assert ("blocking", True) in note.segments()
    protocol = next(h for h in hits if h.path == PROTOCOL_PATH and h.kind == "file")
    assert protocol.root_label == "Lab Drive"
    assert protocol.title == "Western blot: blocking and antibody incubation"
    # Files with every word come before files with only some of them.
    full = [h for h in hits if h.matched_all]
    assert hits[: len(full)] == full
    assert {h.path for h in full} >= {
        "Papers/Blocking_buffer_comparison.pdf",
        "Notes/lab_meeting_2026-10-14.md",
    }
    assert not protocol.matched_all  # its text has "blocking" but not "buffer"


def test_record_hits_rank_above_equal_file_hits(conn: sqlite3.Connection, tmp_path: Path) -> None:
    upsert_root(conn, ROOT)
    text = "Incubation time was raised to two hours."
    upsert_file(conn, FileInfo(ROOT.id, "a.txt", 1, 1.0, ".txt", title=""), text)
    store = RecordStore.init(tmp_path / "record")
    store.append(entry(text=text))
    sync_records(conn, store)
    hits = search(conn, "incubation hours")
    assert [h.kind for h in hits] == ["entry", "file"]


def test_file_snippet_prefers_text_over_path(
    lab_index: tuple[sqlite3.Connection, RecordStore],
) -> None:
    conn, _ = lab_index
    pdf = next(h for h in search(conn, "blocking buffer") if h.path.endswith(".pdf"))  # type: ignore[union-attr]
    assert "comparison.pdf" not in pdf.plain_snippet
    assert f"{HIGHLIGHT_START}Blocking{HIGHLIGHT_END}" in pdf.snippet


def test_snippet_rendering(lab_index: tuple[sqlite3.Connection, RecordStore]) -> None:
    conn, _ = lab_index
    hit = search(conn, "background")[0]
    assert "[background]" in hit.snippet_with("[", "]")
    assert HIGHLIGHT_START not in hit.plain_snippet


@pytest.mark.parametrize(
    "query",
    [
        '"',
        '""',
        '"unbalanced phrase',
        "*",
        "-",
        ":",
        "a:b",
        "NOT",
        "AND OR",
        "blocking AND",
        "(",
        "milk)",
        "anti-pERK",
        "-milk",
        "body:milk",
        "^start",
        "NEAR(a b)",
        "'single'",
        "%_",
        "\\",
        "\x00",
        "🧪 antibody",
        "   ",
        "",
    ],
)
def test_special_characters_never_raise(
    lab_index: tuple[sqlite3.Connection, RecordStore], query: str
) -> None:
    conn, _ = lab_index
    search(conn, query)


def test_phrases(lab_index: tuple[sqlite3.Connection, RecordStore]) -> None:
    conn, _ = lab_index
    paths = {h.path for h in search(conn, '"milk caused high background"')}
    assert "Papers/Blocking_buffer_comparison.pdf" in paths
    assert search(conn, '"background high milk caused"') == []


def test_hyphenated_and_prefix(lab_index: tuple[sqlite3.Connection, RecordStore]) -> None:
    conn, _ = lab_index
    assert any(h.path == "Inventory/Antibody_lot_log_2026.xlsx" for h in search(conn, "anti-pERK"))
    assert any(h.path == "Inventory/Antibody_lot_log_2026.xlsx" for h in search(conn, "discontin"))


def test_unicode_query_matches_unicode_folder(
    lab_index: tuple[sqlite3.Connection, RecordStore],
) -> None:
    conn, _ = lab_index
    target = "Protokolle_Ü/Färbung ñ v2.md"
    assert [h.path for h in search(conn, "Färbeprotokoll")] == [target]
    assert [h.path for h in search(conn, "farbeprotokoll")] == [target]
    assert target in [h.path for h in search(conn, "Protokolle_Ü")]


def test_missing_files_and_unsearched_roots_are_hidden(
    lab_index: tuple[sqlite3.Connection, RecordStore],
) -> None:
    conn, _ = lab_index
    with conn:
        conn.execute("UPDATE files SET status = 'missing' WHERE path LIKE 'Papers/%'")
    assert all(h.path != "Papers/Blocking_buffer_comparison.pdf" for h in search(conn, "milk"))
    with conn:
        conn.execute("UPDATE roots SET search = 0")
    assert [h.kind for h in search(conn, "blocking")] == ["entry"]


def test_project_filter(conn: sqlite3.Connection, tmp_path: Path) -> None:
    upsert_root(conn, ROOT)
    for path in ["ProjA/plan.md", "ProjA_old/plan.md", "Other/plan.md"]:
        upsert_file(conn, FileInfo(ROOT.id, path, 1, 1.0, ".md"), "western plan")
    with conn:
        conn.execute("INSERT INTO project_roots VALUES ('proj_A', ?, 'ProjA')", (ROOT.id,))
    store = RecordStore.init(tmp_path / "record")
    store.append(entry(text="western plan agreed", project_id="proj_A"))
    store.append(entry(text="western plan elsewhere"))
    sync_records(conn, store)
    hits = search(conn, "western plan", project="proj_A")
    assert sorted((h.kind, h.path or h.title) for h in hits) == [
        ("entry", "western plan agreed"),
        ("file", "ProjA/plan.md"),
    ]


def test_limit(lab_index: tuple[sqlite3.Connection, RecordStore]) -> None:
    conn, _ = lab_index
    assert len(search(conn, "a b c blocking milk antibody", limit=2)) == 2
    assert search(conn, "blocking", limit=0) == []


def test_query_parsing() -> None:
    assert parse_query('blocking "milk powder" -') == [
        ("blocking", False),
        ("milk powder", True),
    ]
    assert build_match('blocking "milk powder"') == '"blocking" "milk powder"'
    assert build_match("blocking buff") == '"blocking" "buff"*'
    assert build_match('say "hip') == '"say" "hip"*'
    assert build_match("say hi") == '"say" "hi"'  # too short to be a useful prefix
    assert build_match('a"b') == '"a""b"*'
    assert build_match("x y", any_term=True) == '"x" OR "y"'
    assert build_match("- * :") is None


_WORD_TEXT = """
antibody blocking buffer incubation membrane western blot lot gel transfer primary secondary
dilution wash tbst bsa milk signal band ratio normalize protocol sample plate reader assay
elisa pcr primer cycle lysis protein kinase phospho
"""
WORDS = _WORD_TEXT.split()


@pytest.mark.perf
def test_search_2000_files_under_100ms(conn: sqlite3.Connection) -> None:
    rng = random.Random(42)  # noqa: S311 - test data, not security
    upsert_root(conn, ROOT)
    with conn:
        for i in range(2000):
            body = " ".join(rng.choice(WORDS) for _ in range(400))
            upsert_file(conn, FileInfo(ROOT.id, f"docs/{i:04d}.md", 1, 1.0, ".md"), body)
    queries = ["blocking buffer", "western", '"primary antibody"', "phosph", "elisa plate"]
    search(conn, "warm up")
    for q in queries:
        start = time.perf_counter()
        hits = search(conn, q)
        elapsed = time.perf_counter() - start
        assert hits
        assert elapsed < 0.1, f"{q!r} took {elapsed * 1000:.0f} ms"
