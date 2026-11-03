"""Files and their latest text in the index, plus the text versions seen for each file."""

import sqlite3
from dataclasses import fields, replace

from eridi.core.models import FileInfo, Root, utc_now
from eridi.core.snapshots import VersionLine, replay

_FILE_COLUMNS = [f.name for f in fields(FileInfo) if f.name != "id"]


def upsert_root(conn: sqlite3.Connection, root: Root) -> None:
    conn.execute(
        "INSERT INTO roots (id, kind, label, location, search, save, is_home_default) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET kind = excluded.kind, "
        "label = excluded.label, location = excluded.location, search = excluded.search, "
        "save = excluded.save, is_home_default = excluded.is_home_default",
        (
            root.id,
            root.kind,
            root.label,
            root.location,
            int(root.search),
            int(root.save),
            int(root.is_home_default),
        ),
    )


def list_roots(conn: sqlite3.Connection) -> list[Root]:
    rows = conn.execute("SELECT * FROM roots ORDER BY label, id").fetchall()
    return [
        Root(
            id=r["id"],
            label=r["label"],
            location=r["location"],
            kind=r["kind"],
            search=bool(r["search"]),
            save=bool(r["save"]),
            is_home_default=bool(r["is_home_default"]),
        )
        for r in rows
    ]


def _row_to_file(row: sqlite3.Row) -> FileInfo:
    return FileInfo(
        id=row["id"],
        root_id=row["root_id"],
        path=row["path"],
        size=row["size"],
        modified=row["modified"],
        ext=row["ext"],
        content_sha256=row["content_sha256"],
        text_sha256=row["text_sha256"],
        status=row["status"],
        placeholder=bool(row["placeholder"]),
        title=row["title"],
        extract_status=row["extract_status"],
        last_seen=row["last_seen"],
    )


def get_file(conn: sqlite3.Connection, root_id: str, path: str) -> FileInfo | None:
    row = conn.execute(
        "SELECT * FROM files WHERE root_id = ? AND path = ?", (root_id, path)
    ).fetchone()
    return None if row is None else _row_to_file(row)


def list_files(conn: sqlite3.Connection, root_id: str | None = None) -> list[FileInfo]:
    if root_id is None:
        rows = conn.execute("SELECT * FROM files ORDER BY root_id, path").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM files WHERE root_id = ? ORDER BY path", (root_id,)
        ).fetchall()
    return [_row_to_file(r) for r in rows]


def upsert_file(
    conn: sqlite3.Connection, info: FileInfo, text: str | None, *, source: str = "scan"
) -> FileInfo:
    """Insert or update a file and its searchable text.

    `text=None` keeps the text already indexed (for a file whose content didn't change).
    A new `text_sha256` is added to the file's text versions. Returns the stored file.
    """
    info = replace(info, last_seen=info.last_seen or utc_now())
    values = [getattr(info, c) for c in _FILE_COLUMNS]
    values[_FILE_COLUMNS.index("placeholder")] = int(info.placeholder)
    columns = ", ".join(_FILE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _FILE_COLUMNS)
    row = conn.execute(
        f"INSERT INTO files ({columns}) VALUES ({', '.join('?' * len(values))}) "  # noqa: S608
        f"ON CONFLICT (root_id, path) DO UPDATE SET {updates} RETURNING id",
        values,
    ).fetchone()
    file_id = int(row[0])

    if text is None:
        old = conn.execute("SELECT body FROM files_fts WHERE rowid = ?", (file_id,)).fetchone()
        body = "" if old is None else str(old[0])
    else:
        body = text
    conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
    conn.execute(
        "INSERT INTO files_fts (rowid, path, title, body) VALUES (?, ?, ?, ?)",
        (file_id, info.path, info.title or "", body),
    )

    if info.text_sha256 is not None:
        conn.execute(
            "INSERT OR IGNORE INTO text_versions (file_id, text_sha256, first_seen, source) "
            "VALUES (?, ?, ?, ?)",
            (file_id, info.text_sha256, info.last_seen, source),
        )
    return replace(info, id=file_id)


def set_status(conn: sqlite3.Connection, root_id: str, path: str, status: str) -> None:
    """Set a file's status. A missing file leaves the full-text index (its row and history
    stay), so search statistics match a rebuild, which can't see files that are gone. If the
    file comes back it must be re-read, since `upsert_file(text=None)` has no text to keep."""
    row = conn.execute(
        "UPDATE files SET status = ? WHERE root_id = ? AND path = ? RETURNING id",
        (status, root_id, path),
    ).fetchone()
    if row is not None and status == "missing":
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (row[0],))


def file_text(conn: sqlite3.Connection, file_id: int) -> str | None:
    row = conn.execute("SELECT body FROM files_fts WHERE rowid = ?", (file_id,)).fetchone()
    return None if row is None else str(row[0])


def text_versions(conn: sqlite3.Connection, file_id: int) -> list[tuple[str, str, str]]:
    """(text_sha256, first_seen, source) for each text version, oldest first."""
    rows = conn.execute(
        "SELECT text_sha256, first_seen, source FROM text_versions WHERE file_id = ? "
        "ORDER BY first_seen, rowid",
        (file_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def file_moves(conn: sqlite3.Connection, file_id: int) -> list[tuple[str, str, str]]:
    """(from_path, to_path, moved_at) for each move of a file, oldest first."""
    rows = conn.execute(
        "SELECT from_path, to_path, moved_at FROM file_moves WHERE file_id = ? "
        "ORDER BY moved_at, rowid",
        (file_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def restore_versions(conn: sqlite3.Connection, lines: list[VersionLine]) -> None:
    """Replace `text_versions` and `file_moves` with what the version log says.

    Used by rebuilds: the log is the durable source, the tables are a cache of it.
    """
    versions, moves = replay(lines)
    ids = {(r[0], r[1]): int(r[2]) for r in conn.execute("SELECT root_id, path, id FROM files")}
    conn.execute("DELETE FROM text_versions")
    conn.execute("DELETE FROM file_moves")
    for (root_id, path), by_sha in versions.items():
        file_id = ids.get((root_id, path))
        if file_id is None:
            continue
        conn.executemany(
            "INSERT INTO text_versions (file_id, text_sha256, first_seen, source) "
            "VALUES (?, ?, ?, ?)",
            [(file_id, v.text_sha256, v.first_seen, v.source) for v in by_sha.values()],
        )
    # Each move belongs to the file where the chain of moves ends.
    for i, move in enumerate(moves):
        dest = move.path
        for later in moves[i + 1 :]:
            if later.root_id == move.root_id and later.from_path == dest:
                dest = later.path
        file_id = ids.get((move.root_id, dest))
        if file_id is not None:
            conn.execute(
                "INSERT INTO file_moves (file_id, from_path, to_path, moved_at) "
                "VALUES (?, ?, ?, ?)",
                (file_id, move.from_path, move.path, move.at),
            )
    # Current text that the log doesn't know (e.g. a log lost with old app data) still counts.
    conn.execute(
        "INSERT OR IGNORE INTO text_versions (file_id, text_sha256, first_seen, source) "
        "SELECT id, text_sha256, last_seen, 'scan' FROM files WHERE text_sha256 IS NOT NULL"
    )
