"""The SQLite index: open it, check FTS5, create or rebuild the schema.

The index is disposable. Everything in it can be rebuilt from the storage folders and the
record, so when the schema version changes the tables are simply dropped and recreated.
"""

import sqlite3
from pathlib import Path

from eridi.core.paths import app_home

SCHEMA_VERSION = 3
INDEX_NAME = "index.sqlite"

# Porter stemming so "blocking" finds "block"; diacritics folded so "Farbung" finds "Färbung".
TOKENIZER = "porter unicode61 remove_diacritics 2"

SCHEMA = f"""
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE roots (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    location TEXT NOT NULL,
    search INTEGER NOT NULL DEFAULT 1,
    save INTEGER NOT NULL DEFAULT 0,
    is_home_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    root_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified REAL NOT NULL,
    content_sha256 TEXT,
    text_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'present',
    ext TEXT NOT NULL,
    placeholder INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL,
    title TEXT,
    extract_status TEXT,
    UNIQUE (root_id, path)
);
CREATE INDEX files_content ON files (content_sha256);
CREATE TABLE text_versions (
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    text_sha256 TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (file_id, text_sha256)
);
CREATE TABLE file_moves (
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    from_path TEXT NOT NULL,
    to_path TEXT NOT NULL,
    moved_at TEXT NOT NULL
);
CREATE INDEX file_moves_file ON file_moves (file_id);
CREATE VIRTUAL TABLE files_fts USING fts5(path, title, body, tokenize = '{TOKENIZER}');
CREATE TABLE entries (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    project_id TEXT,
    outcome TEXT,
    text TEXT NOT NULL,
    supersedes TEXT,
    target_id TEXT,
    change_to_text_sha256 TEXT,
    state TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX entries_project ON entries (project_id);
CREATE INDEX entries_change ON entries (change_to_text_sha256);
CREATE TABLE entry_subjects (
    entry_id TEXT NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
    root_id TEXT NOT NULL,
    path TEXT NOT NULL,
    text_sha256 TEXT
);
CREATE INDEX entry_subjects_file ON entry_subjects (root_id, path);
CREATE INDEX entry_subjects_text ON entry_subjects (text_sha256);
CREATE VIRTUAL TABLE entries_fts USING fts5(text, tags, summary, tokenize = '{TOKENIZER}');
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    home_root_id TEXT
);
CREATE TABLE cloud_items (
    root_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    parent_id TEXT,
    name TEXT NOT NULL,
    is_folder INTEGER NOT NULL,
    size INTEGER NOT NULL,
    modified REAL NOT NULL,
    is_root INTEGER NOT NULL,
    PRIMARY KEY (root_id, item_id)
);
CREATE TABLE project_roots (
    project_id TEXT NOT NULL,
    root_id TEXT NOT NULL,
    path_prefix TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, root_id, path_prefix)
);
"""


class IndexUnavailableError(RuntimeError):
    """SQLite on this computer can't host the index (for example, no FTS5)."""


def index_path() -> Path:
    return app_home() / INDEX_NAME


def check_fts5() -> None:
    """Raise a clear error if this Python's SQLite lacks full-text search (FTS5)."""
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError:
        raise IndexUnavailableError(
            f"This Python's SQLite ({sqlite3.sqlite_version}) has no full-text search (FTS5). "
            "Install Python from python.org or with `uv python install`, which include it."
        ) from None
    finally:
        probe.close()


def _existing_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return None
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def drop_all(conn: sqlite3.Connection) -> None:
    """Remove every table, index and view (the index is disposable)."""
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "AND type IN ('table', 'view')"
    ).fetchall()
    with conn:
        for kind, name in rows:
            # FTS5 shadow tables disappear with their virtual table.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
            ).fetchone()
            if exists:
                conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')


def create_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )


def open_index(path: Path | None = None, *, threads: bool = False) -> sqlite3.Connection:
    """Open (creating or rebuilding as needed) the index database.

    `path=None` uses `<app-data>/index.sqlite`; pass `Path(":memory:")` for a throwaway index.
    `threads=True` allows use from several threads; the caller must serialize access.
    """
    check_fts5()
    path = path or index_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # Another Eridi (the app and a terminal) may be writing; wait for it rather than fail.
    conn = sqlite3.connect(path, check_same_thread=not threads, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        version = _existing_version(conn)
        if version != SCHEMA_VERSION:
            drop_all(conn)
            create_schema(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        if "not a database" in str(exc) or "malformed" in str(exc):
            # A damaged index file: it's disposable, so start a fresh one.
            for suffix in ("", "-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            return open_index(path, threads=threads)
        raise
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
