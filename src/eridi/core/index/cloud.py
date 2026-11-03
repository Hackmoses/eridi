"""Cloud drives' item trees and delta cursors, kept in the index between scans.

Like everything in the index this is disposable: without it, the next scan lists the
drive from the start. The cursor is saved with the items it belongs to, in one
transaction, so the two can never disagree.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from eridi.connectors.tree import CloudItem
from eridi.core.index.db import get_meta, open_index, set_meta

CURSOR_PREFIX = "delta_cursor:"


class IndexDeltaCache:
    """A `DeltaCache` in the index database. Opens its own connection per call (scans run
    in their own thread), unless given a shared one (an in-memory index)."""

    def __init__(self, index_file: Path | None = None, conn: sqlite3.Connection | None = None):
        self._connect: Callable[[], sqlite3.Connection] = (
            (lambda: conn) if conn is not None else (lambda: open_index(index_file))
        )
        self._shared = conn is not None

    def _run[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            return work(conn)
        finally:
            if not self._shared:
                conn.close()

    def load(self, root_id: str) -> tuple[str | None, dict[str, CloudItem]]:
        def read(conn: sqlite3.Connection) -> tuple[str | None, dict[str, CloudItem]]:
            rows = conn.execute(
                "SELECT item_id, parent_id, name, is_folder, size, modified, is_root, mime "
                "FROM cloud_items WHERE root_id = ?",
                (root_id,),
            ).fetchall()
            items = {
                r[0]: CloudItem(r[0], r[1], r[2], bool(r[3]), r[4], r[5], bool(r[6]), r[7])
                for r in rows
            }
            return get_meta(conn, CURSOR_PREFIX + root_id), items

        return self._run(read)

    def save(
        self, root_id: str, cursor: str, upserts: list[CloudItem], removed: list[str], *,
        reset: bool,
    ) -> None:  # fmt: skip
        def write(conn: sqlite3.Connection) -> None:
            with conn:
                if reset:
                    conn.execute("DELETE FROM cloud_items WHERE root_id = ?", (root_id,))
                conn.executemany(
                    "DELETE FROM cloud_items WHERE root_id = ? AND item_id = ?",
                    [(root_id, i) for i in removed],
                )
                conn.executemany(
                    "INSERT INTO cloud_items (root_id, item_id, parent_id, name, is_folder, "
                    "size, modified, is_root, mime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (root_id, item_id) DO UPDATE SET parent_id = excluded.parent_id, "
                    "name = excluded.name, is_folder = excluded.is_folder, size = excluded.size, "
                    "modified = excluded.modified, is_root = excluded.is_root, "
                    "mime = excluded.mime",
                    [
                        (root_id, i.id, i.parent_id, i.name, int(i.is_folder), i.size, i.modified,
                         int(i.is_root), i.mime)
                        for i in upserts
                    ],
                )  # fmt: skip
                set_meta(conn, CURSOR_PREFIX + root_id, cursor)

        self._run(write)

    def forget(self, root_id: str) -> None:
        """Drop a drive's tree and cursor (it was disconnected)."""

        def drop(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute("DELETE FROM cloud_items WHERE root_id = ?", (root_id,))
                conn.execute("DELETE FROM meta WHERE key = ?", (CURSOR_PREFIX + root_id,))

        self._run(drop)
