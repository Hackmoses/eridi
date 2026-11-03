"""M3.5: the index is disposable. A rebuild equals an incrementally maintained index."""

import sqlite3
from pathlib import Path
from typing import Any

from docx import Document

from conftest import SampleLab, entry
from eridi.connectors.local import LocalConnector
from eridi.core.config import Settings
from eridi.core.index.db import open_index
from eridi.core.index.ingest import index_root
from eridi.core.index.rebuild import rebuild
from eridi.core.index.records import sync_records
from eridi.core.index.search import search
from eridi.core.models import Root, Subject
from eridi.core.record.store import RecordStore
from eridi.core.snapshots import SnapshotStore

QUERIES = [
    "blocking buffer",
    "antibody",
    "incubation",
    '"primary antibody incubation"',
    "Färbeprotokoll",
    "milk",
    "GAPDH",
    "overnight",
    "retracted",
    "lot",
    "anti-pERK",
    "notebook",
]
SETTINGS = Settings()


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything that affects what users see, minus index-internal row IDs and timestamps."""
    return {
        "search": {
            q: [
                (h.kind, h.id, h.title, h.snippet, round(h.score, 9), h.matched_all)
                for h in search(conn, q, 50)
            ]
            for q in QUERIES
        },
        "files": [
            tuple(r)
            for r in conn.execute(
                "SELECT root_id, path, size, modified, content_sha256, text_sha256, status, ext, "
                "placeholder, title, extract_status FROM files WHERE status = 'present' "
                "ORDER BY root_id, path"
            )
        ],
        "entries": [
            tuple(r) for r in conn.execute("SELECT id, kind, state, text FROM entries ORDER BY id")
        ],
        "subjects": sorted(tuple(r) for r in conn.execute("SELECT * FROM entry_subjects")),
        "projects": [tuple(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")],
        "roots": [tuple(r) for r in conn.execute("SELECT * FROM roots ORDER BY id")],
        "versions": sorted(
            tuple(r)
            for r in conn.execute(
                "SELECT f.root_id, f.path, v.text_sha256, v.first_seen, v.source "
                "FROM text_versions v JOIN files f ON f.id = v.file_id WHERE f.status = 'present'"
            )
        ),
    }


def test_rebuild_equals_incremental(sample_lab: SampleLab, tmp_path: Path) -> None:
    root = Root("root_LAB", "Lab Drive", str(sample_lab.root))
    connector = LocalConnector(sample_lab.root, root.id)
    store = RecordStore.init(tmp_path / "record")
    snaps = SnapshotStore()
    incremental = open_index(tmp_path / "incremental.sqlite")

    # Day 1: first index, a note.
    index_root(incremental, root, connector, size_limit=SETTINGS.size_limit_bytes, snapshots=snaps)
    protocol = Subject(root.id, "Protocols/Western_blot_blocking_v7.docx")
    first = entry(
        text="Primary antibody incubation is 1 h because of the old lot.", subjects=[protocol]
    )
    store.append(first)
    sync_records(incremental, store)

    # Day 2: the protocol changes, a file is deleted, a note is corrected, another retracted.
    doc = Document(str(sample_lab.protocol_docx))
    doc.add_paragraph("Primary antibody incubation: overnight at 4 °C.")
    doc.save(str(sample_lab.protocol_docx))
    sample_lab.analysis_r.unlink()
    (sample_lab.root / "Notes" / "new_notebook_entry.md").write_text(
        "# Notebook\nLot B2291 behaved well.\n", encoding="utf-8"
    )
    index_root(incremental, root, connector, size_limit=SETTINGS.size_limit_bytes, snapshots=snaps)
    store.append(
        entry(
            text="Incubation is now overnight at 4 °C; signal restored.",
            subjects=[protocol],
            supersedes=first.id,
        )
    )
    wrong = entry(text="This note was retracted: wrong protocol.")
    store.append(wrong)
    sync_records(incremental, store)
    store.append(entry("tombstone", "", target_id=wrong.id))
    sync_records(incremental, store)

    rebuilt = open_index(tmp_path / "rebuilt.sqlite")
    report = rebuild(rebuilt, [root], store, SETTINGS, snaps)
    assert report.unavailable == {}
    assert report.record is not None and report.record.full

    before = snapshot(incremental)
    after = snapshot(rebuilt)
    assert before == after
    # Only the incremental index remembers the deleted file (as missing, not searchable).
    statuses = dict(incremental.execute("SELECT path, status FROM files").fetchall())
    assert statuses["Analysis/densitometry_analysis.R"] == "missing"
    assert before["search"]["overnight"]  # the comparison isn't vacuous
    protocol_versions = [v for v in before["versions"] if v[1] == protocol.path]
    assert len(protocol_versions) == 2  # history survived the rebuild
    assert ("file", "root_LAB:Analysis/densitometry_analysis.R") not in [
        h[:2] for h in before["search"]["GAPDH"]
    ]

    # Rebuilding in place, over the existing index, gives the same again.
    rebuild(incremental, [root], store, SETTINGS, snaps)
    assert snapshot(incremental) == after
    incremental.close()
    rebuilt.close()


def test_rebuild_skips_unsearched_and_unavailable_roots(
    tmp_path: Path, sample_lab: SampleLab
) -> None:
    conn = open_index(Path(":memory:"))
    roots = [
        Root("root_LAB", "Lab Drive", str(sample_lab.root), search=False),
        Root("root_USB", "USB stick", str(tmp_path / "unplugged")),
        Root("root_CLOUD", "Future cloud", "drive-id", kind="dropbox"),
    ]
    report = rebuild(conn, roots, None)
    assert set(report.unavailable) == {"root_USB", "root_CLOUD"}
    assert "can't be found" in report.unavailable["root_USB"]
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert {r[0] for r in conn.execute("SELECT id FROM roots")} >= {"root_LAB", "root_USB"}
    assert search(conn, "blocking") == []
