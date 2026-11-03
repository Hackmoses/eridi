"""Load record entries into the index: `entries`, `entry_subjects`, `entries_fts`, `projects`.

Every entry is stored with its state (current, superseded or retracted); only current,
non-tombstone entries are searchable. Loading is incremental: only entry files with an ID
above the highest one seen are read. Because sync can deliver an older entry late, a full
reconcile runs periodically (and whenever asked), which also drops entries whose files are
gone.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from eridi.core.index.db import get_meta, set_meta
from eridi.core.models import Entry, ProjectFolder, format_timestamp, parse_timestamp
from eridi.core.record.schema import parse_entry, serialize_entry
from eridi.core.record.store import RecordProblem, RecordStore
from eridi.core.record.views import resolve

LAST_ENTRY_KEY = "record_last_entry_id"
LAST_RECONCILE_KEY = "record_last_reconcile"
RECONCILE_INTERVAL = timedelta(hours=1)


@dataclass
class RecordSyncReport:
    full: bool
    added: int = 0
    added_ids: list[str] = field(default_factory=list)
    removed: int = 0
    problems: list[RecordProblem] = field(default_factory=list)
    newer_version: int = 0


def _searchable(entry: Entry, state: str) -> bool:
    # Project entries hold no words of their own; projects are listed, not searched.
    return state == "current" and entry.kind not in ("tombstone", "project")


def _fts_values(entry: Entry) -> tuple[str, str, str]:
    summary_parts = [entry.change.summary] if entry.change and entry.change.summary else []
    if entry.project is not None:
        summary_parts.append(entry.project.name)
    # File names make "notes about WB_v7" findable by the file's name.
    summary_parts += [s.path for s in entry.subjects]
    return entry.text, " ".join(entry.tags), "\n".join(summary_parts)


def _insert(conn: sqlite3.Connection, entry: Entry) -> bool:
    """Add an entry. False if it's already there: another Eridi process sharing this index
    (the web app and `eridi watch`, say) can load the same new entry at the same moment."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO entries (id, kind, created_at, author_name, author_kind, "
        "project_id, outcome, text, supersedes, target_id, change_to_text_sha256, state, json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?)",
        (
            entry.id,
            entry.kind,
            entry.created_at,
            entry.author.name,
            entry.author.kind,
            entry.project_id,
            entry.outcome,
            entry.text,
            entry.supersedes,
            entry.target_id,
            entry.change.to_text_sha256 if entry.change else None,
            serialize_entry(entry),
        ),
    )
    if cursor.rowcount == 0:
        return False
    conn.executemany(
        "INSERT INTO entry_subjects (entry_id, root_id, path, text_sha256) VALUES (?, ?, ?, ?)",
        [(entry.id, s.root_id, s.path, s.text_sha256) for s in entry.subjects],
    )
    return True


def _delete(conn: sqlite3.Connection, entry_id: str) -> None:
    row = conn.execute("SELECT seq FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (row[0],))
    conn.execute("DELETE FROM entry_subjects WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))


def _load_all(conn: sqlite3.Connection) -> list[tuple[int, str, Entry]]:
    rows = conn.execute("SELECT seq, state, json FROM entries ORDER BY id").fetchall()
    return [(r[0], r[1], parse_entry(json.loads(r[2]))) for r in rows]


def _refresh_states(conn: sqlite3.Connection, new_ids: set[str]) -> None:
    """Recompute every entry's state; update search rows that changed; rebuild projects."""
    loaded = _load_all(conn)
    items = resolve(e for _, _, e in loaded)
    for seq, old_state, entry in loaded:
        state = items[entry.id].state
        if state == old_state and entry.id not in new_ids:
            continue
        conn.execute("UPDATE entries SET state = ? WHERE seq = ?", (state, seq))
        conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (seq,))
        if _searchable(entry, state):
            conn.execute(
                "INSERT INTO entries_fts (rowid, text, tags, summary) VALUES (?, ?, ?, ?)",
                (seq, *_fts_values(entry)),
            )

    conn.execute("DELETE FROM projects")
    conn.execute("DELETE FROM project_roots")
    folders: dict[str, tuple[ProjectFolder, ...]] = {}
    for _, _, entry in loaded:  # oldest first, so the latest project entry wins
        if entry.kind == "project" and entry.project and items[entry.id].state == "current":
            p = entry.project
            conn.execute(
                "INSERT INTO projects (id, name, home_root_id) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
                "home_root_id = excluded.home_root_id",
                (p.id, p.name, p.home_root_id),
            )
            if p.folders is not None:  # an entry that doesn't list folders keeps them
                folders[p.id] = p.folders
    conn.executemany(
        "INSERT OR IGNORE INTO project_roots (project_id, root_id, path_prefix) VALUES (?, ?, ?)",
        [(pid, f.root_id, f.path) for pid, fs in folders.items() for f in fs],
    )


def _known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM entries")}


def _update_last_id(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT max(id) FROM entries").fetchone()
    if row[0] is not None:
        set_meta(conn, LAST_ENTRY_KEY, row[0])


def _needs_full(conn: sqlite3.Connection, now: datetime) -> bool:
    if get_meta(conn, LAST_ENTRY_KEY) is None:
        return True
    last = get_meta(conn, LAST_RECONCILE_KEY)
    if last is None:
        return True
    try:
        return now - parse_timestamp(last) >= RECONCILE_INTERVAL
    except ValueError:
        return True


def sync_records(
    conn: sqlite3.Connection,
    store: RecordStore,
    *,
    full: bool | None = None,
    now: datetime | None = None,
) -> RecordSyncReport:
    """Bring the index up to date with the record folder.

    `full=None` decides automatically: a full reconcile on first load and then at most
    every `RECONCILE_INTERVAL`; otherwise only entries newer than the last one seen.
    """
    now = now or datetime.now(UTC)
    if full is None:
        full = _needs_full(conn, now)
    report = RecordSyncReport(full=full)
    after = None if full else get_meta(conn, LAST_ENTRY_KEY)
    read = store.read(after=after)
    report.problems = read.problems
    report.newer_version = len(read.newer_version)

    known = _known_ids(conn)
    new = [e for e in read.entries if e.id not in known]
    with conn:
        if full:
            present = {e.id for e in read.entries}
            gone = known - present
            for entry_id in gone:
                _delete(conn, entry_id)
            report.removed = len(gone)
        new = [e for e in new if _insert(conn, e)]
        report.added = len(new)
        report.added_ids = [e.id for e in new]
        if new or report.removed:
            _refresh_states(conn, {e.id for e in new})
        _update_last_id(conn)
        if full:
            set_meta(conn, LAST_RECONCILE_KEY, format_timestamp(now))
    return report
