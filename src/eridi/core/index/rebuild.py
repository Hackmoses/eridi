"""Throw the index away and build it again from the storage roots and the record.

The index is disposable: a rebuild must give the same search results as an index that
was kept up to date step by step. `tests/test_index_rebuild.py` enforces this.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from eridi.connectors import connector_for
from eridi.connectors.base import Connector, ConnectorError
from eridi.core.config import Settings
from eridi.core.index.db import create_schema, drop_all
from eridi.core.index.files import restore_versions, upsert_root
from eridi.core.index.records import RecordSyncReport, sync_records
from eridi.core.models import Root
from eridi.core.record.store import RecordStore
from eridi.core.scan import ScanReport, scan_root
from eridi.core.snapshots import SnapshotStore


@dataclass
class RebuildReport:
    roots: dict[str, ScanReport] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    """Root ID → why it could not be indexed (for example, a drive that isn't connected)."""
    record: RecordSyncReport | None = None


def rebuild(
    conn: sqlite3.Connection,
    roots: Iterable[Root],
    store: RecordStore | None,
    snapshots: SnapshotStore,
    settings: Settings | None = None,
    connectors: dict[str, Connector] | None = None,
) -> RebuildReport:
    """Drop every table, index each root with search on, restore version history from the
    snapshot log, then load the whole record."""
    settings = settings or Settings()
    report = RebuildReport()
    drop_all(conn)
    create_schema(conn)
    for root in roots:
        if not root.search:
            with conn:
                upsert_root(conn, root)
            continue
        try:
            connector = (connectors or {}).get(root.id) or connector_for(root, settings)
        except ConnectorError as exc:
            report.unavailable[root.id] = str(exc)
            with conn:
                upsert_root(conn, root)
            continue
        scanned = scan_root(conn, root, connector, snapshots, settings)
        report.roots[root.id] = scanned
        if scanned.unavailable:
            report.unavailable[root.id] = scanned.unavailable
    with conn:
        restore_versions(conn, snapshots.read_log())
    if store is not None:
        report.record = sync_records(conn, store, full=True)
    return report
