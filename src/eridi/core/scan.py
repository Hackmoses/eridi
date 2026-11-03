"""Scan storage roots: find new, changed, moved and removed files, extract their text and
snapshot every new text version.

Cheap by design: a file is re-read only when its size or modified time changed, and its
text is extracted only when its bytes changed. Files nothing can be extracted from
(photos, videos, archives) are indexed by name without being read. Removed files are marked
missing, never deleted, so their history stays. A file that disappears while another file
with the same bytes appears is a move, and its history follows it.

Fast on big folders: reading and extracting run in parallel worker processes, one per CPU
core (`settings.scan_workers`), while this process writes the index. Results commit in
small batches, so search works on what's done while a long scan carries on, and an
interrupted scan keeps its progress.
"""

import concurrent.futures as futures
import contextlib
import logging
import multiprocessing
import os
import pickle
import sqlite3
import sys
import time
import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Literal

from eridi.connectors.base import Connector, ConnectorError, RemoteFile
from eridi.core.config import Settings
from eridi.core.extract import ExtractResult, extension, extract, extractor_for
from eridi.core.hashing import sha256_bytes, sha256_text
from eridi.core.index.files import (
    get_file,
    list_files,
    move_file,
    set_status,
    upsert_file,
    upsert_root,
)
from eridi.core.models import FileInfo, Root, utc_now
from eridi.core.snapshots import SnapshotStore

log = logging.getLogger(__name__)

COMMIT_EVERY = 50
PARALLEL_MIN_FILES = 32
"""Below this many files to read, worker processes cost more than they save."""
MAX_WORKERS = 16
SLOW_SECONDS = 5.0
"""Files taking longer than this to read are listed in the scan report."""
PENDING = "pending"
"""extract_status of a new file whose name is indexed but whose text isn't read yet."""


@dataclass(frozen=True)
class ScanProgress:
    root: Root
    phase: Literal["listing", "reading"]
    done: int
    total: int | None
    """Files to read in this scan; None while listing."""
    path: str
    bytes_done: int = 0
    bytes_total: int = 0


Progress = Callable[[ScanProgress], None]


@dataclass
class ScanReport:
    root_id: str
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    """Files whose text changed."""
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason): too large, online-only."""
    errors: list[tuple[str, str]] = field(default_factory=list)
    """(path, reason): files that could not be read or extracted."""
    extracted: int = 0
    unavailable: str | None = None
    """Why the whole root could not be scanned (e.g. a drive that isn't connected)."""
    workers: int = 1
    seconds: float = 0.0
    slow: list[tuple[str, float]] = field(default_factory=list)
    """(path, seconds) for the slowest files, slowest first."""


def _stem(path: str) -> str:
    return PurePosixPath(path).stem


def worker_count(settings: Settings) -> int:
    """`scan_workers = 0` means one per CPU core, keeping one free for everything else."""
    if settings.scan_workers > 0:
        return min(settings.scan_workers, MAX_WORKERS)
    return max(1, min((os.cpu_count() or 2) - 1, MAX_WORKERS))


def read_bytes(
    connector: Connector, remote: RemoteFile, size_limit: int | None, *, read_placeholders: bool
) -> tuple[bytes | None, ExtractResult | None]:
    """The file's bytes, or (None, a result saying why it wasn't read). Never raises."""
    stem = _stem(remote.path)
    if remote.is_placeholder and not read_placeholders:
        return None, ExtractResult("", stem, "online_only", "Online-only; not downloaded.")
    if size_limit is not None and remote.size > size_limit:
        mb = size_limit / (1024 * 1024)
        return None, ExtractResult("", stem, "too_large", f"Larger than the {mb:g} MB limit.")
    try:
        return connector.open_bytes(remote.path), None
    except (ConnectorError, OSError, ValueError) as exc:
        return None, ExtractResult("", stem, "error", str(exc))


def read_and_extract(
    connector: Connector, remote: RemoteFile, size_limit: int | None, *, read_placeholders: bool
) -> tuple[str | None, ExtractResult]:
    """(content_sha256, extraction) for one file. Never raises."""
    data, skipped = read_bytes(connector, remote, size_limit, read_placeholders=read_placeholders)
    if data is None:
        assert skipped is not None  # noqa: S101 - read_bytes returns one or the other
        return remote.content_sha256, skipped
    return sha256_bytes(data), extract(remote.path.rsplit("/", 1)[-1], data)


@dataclass(frozen=True)
class Examined:
    """What reading one file found. `result=None` means its bytes hadn't changed."""

    content_sha256: str | None
    result: ExtractResult | None
    read: bool
    """The bytes were read (False: skipped, or the read failed)."""
    extracted: bool
    seconds: float = 0.0
    duplicate: bool = False
    """Same bytes as a file already indexed: its text is copied, not extracted again."""


def examine(
    connector: Connector,
    remote: RemoteFile,
    old_sha: str | None,
    size_limit: int | None,
    read_placeholders: bool,
) -> Examined:
    """Read, hash and (if the bytes changed) extract one file. Runs in worker processes."""
    started = time.perf_counter()
    data, skipped = read_bytes(connector, remote, size_limit, read_placeholders=read_placeholders)
    if data is None:
        return Examined(remote.content_sha256, skipped, read=False, extracted=False)
    sha = sha256_bytes(data)
    if old_sha is not None and sha == old_sha:
        return Examined(sha, None, read=True, extracted=False)
    if sha in _KNOWN:
        return Examined(sha, None, read=True, extracted=False, duplicate=True)
    result = extract(remote.path.rsplit("/", 1)[-1], data)
    return Examined(sha, result, read=True, extracted=True,
                    seconds=time.perf_counter() - started)  # fmt: skip


_KNOWN: frozenset[str] = frozenset()
"""Content hashes whose text is already in the index (set in each worker at start)."""


def _init_worker(known: frozenset[str]) -> None:
    """Runs first in each worker. Parsers' warnings about odd files are noise to users and
    would otherwise print straight to the terminal."""
    global _KNOWN
    _KNOWN = known
    root = logging.getLogger()
    root.handlers[:] = [logging.NullHandler()]
    root.setLevel(logging.CRITICAL)
    warnings.simplefilter("ignore")


def _picklable(connector: Connector) -> bool:
    try:
        pickle.dumps(connector)
    except Exception:
        return False
    return True


Job = tuple[RemoteFile, FileInfo | None]


@contextlib.contextmanager
def _main_module_hidden() -> Iterator[None]:
    """Let worker processes start however Eridi was launched.

    "spawn" workers re-run the parent's main script. That breaks when it isn't an
    importable file: Windows .exe launchers, `python -` and some app bundles. Workers only
    need `eridi.core.scan`, so hide the main script from them while the pool runs.
    """
    main = sys.modules.get("__main__")
    saved = dict(vars(main)) if main is not None else {}
    try:
        if main is not None:
            vars(main).pop("__file__", None)
            main.__spec__ = None
        yield
    finally:
        if main is not None:
            for key in ("__file__", "__spec__"):
                if key in saved:
                    vars(main)[key] = saved[key]


def _examine_all(
    connector: Connector,
    jobs: list[Job],
    settings: Settings,
    report: ScanReport,
    known: frozenset[str] = frozenset(),
) -> Iterator[tuple[RemoteFile, FileInfo | None, Examined]]:
    """Examine every job, in parallel when it's worth it. Yields as files finish.

    Sets `report.workers` to the number of processes that actually did the work."""
    global _KNOWN
    _KNOWN = known  # for files examined in this process
    size_limit = settings.size_limit_bytes
    placeholders = settings.read_online_only_files
    workers = worker_count(settings)

    def here(job: Job) -> Examined:
        remote, old = job
        return examine(connector, remote, old.content_sha256 if old else None, size_limit,
                       placeholders)  # fmt: skip

    if workers <= 1 or len(jobs) < PARALLEL_MIN_FILES or not _picklable(connector):
        report.workers = 1
        for job in jobs:
            yield job[0], job[1], here(job)
        return

    # "spawn" behaves the same on every OS and is safe while other threads are running.
    report.workers = workers
    hidden = _main_module_hidden()
    hidden.__enter__()
    pool = futures.ProcessPoolExecutor(
        workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=(known,),
    )
    queue = iter(jobs)
    running: dict[futures.Future[Examined], Job] = {}
    try:
        while True:
            while len(running) < workers * 4:
                upcoming = next(queue, None)
                if upcoming is None:
                    break
                remote, old = upcoming
                future = pool.submit(examine, connector, remote,
                                     old.content_sha256 if old else None, size_limit,
                                     placeholders)  # fmt: skip
                running[future] = upcoming
            if not running:
                return
            finished, _ = futures.wait(running, return_when=futures.FIRST_COMPLETED)
            for future in finished:
                job = running.pop(future)
                try:
                    examined = future.result()
                except futures.process.BrokenProcessPool:
                    running[future] = job
                    raise
                except Exception:
                    log.warning("A worker failed on %s; reading it here instead.", job[0].path)
                    examined = here(job)
                yield job[0], job[1], examined
    except futures.process.BrokenProcessPool:
        # A worker crashed (e.g. a parser bug on one odd file). Finish without workers.
        log.warning("Scan workers stopped unexpectedly; finishing in one process.")
        report.workers = 1
        pool.shutdown(wait=False, cancel_futures=True)
        for job in [*running.values(), *queue]:
            yield job[0], job[1], here(job)
        return
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        hidden.__exit__(None, None, None)


def scan_root(
    conn: sqlite3.Connection,
    root: Root,
    connector: Connector,
    snapshots: SnapshotStore,
    settings: Settings | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    settings = settings or Settings()
    report = ScanReport(root.id)
    started = time.perf_counter()
    now = utc_now()
    with conn:
        upsert_root(conn, root)
    existing = {f.path: f for f in list_files(conn, root.id)}
    seen: set[str] = set()
    new_files: list[FileInfo] = []
    jobs: list[Job] = []
    pending = 0

    def commit() -> None:
        snapshots.flush()  # the version log first, so the index never runs ahead of it
        conn.commit()

    try:
        for listed, remote in enumerate(connector.list_files()):
            if progress is not None and listed % 200 == 0:
                progress(ScanProgress(root, "listing", listed, None, remote.path))
            seen.add(remote.path)
            old = existing.get(remote.path)
            if _unchanged(old, remote):
                report.unchanged += 1
            elif extractor_for(remote.path) is None:
                # Nothing to search inside it: index its name without reading it.
                name_only = ExtractResult("", _stem(remote.path), "name_only")
                stored = _apply(conn, root, remote, old, Examined(None, name_only, False, False),
                                now, snapshots, report)  # fmt: skip
                if stored is not None:
                    new_files.append(stored)
                pending += 1
            else:
                if old is None or old.extract_status == PENDING:
                    # Searchable by name right away; the text follows when it's read.
                    upsert_file(conn, _pending_row(root, remote, now), None)
                    old = None
                    pending += 1
                jobs.append((remote, old))
            if pending >= COMMIT_EVERY:
                commit()
                pending = 0
    except ConnectorError as exc:
        commit()
        report.unavailable = str(exc)
        return report  # don't mark anything missing when the root itself is unreachable

    commit()  # every new file is now findable by name
    # Smallest first: most files become fully searchable early; big ones come last.
    jobs.sort(key=lambda job: job[0].size)
    bytes_total = sum(job[0].size for job in jobs)
    bytes_done = 0
    known = _texts_known(conn) if jobs else frozenset()
    try:
        for done, (remote, old, examined) in enumerate(
            _examine_all(connector, jobs, settings, report, known)
        ):
            bytes_done += remote.size
            if examined.seconds >= SLOW_SECONDS:
                report.slow.append((remote.path, examined.seconds))
            if progress is not None:
                progress(ScanProgress(root, "reading", done + 1, len(jobs), remote.path,
                                      bytes_done, bytes_total))  # fmt: skip
            stored = _apply(conn, root, remote, old, examined, now, snapshots, report)
            if stored is not None:
                new_files.append(stored)
            pending += 1
            if pending >= COMMIT_EVERY:
                commit()
                pending = 0
    finally:
        commit()  # keep everything finished so far, even when interrupted

    gone = {p: f for p, f in existing.items() if p not in seen and f.status == "present"}
    _settle_gone(conn, root, snapshots, report, gone, new_files, now)
    commit()
    report.seconds = time.perf_counter() - started
    return _sorted(report)


def _settle_gone(
    conn: sqlite3.Connection,
    root: Root,
    snapshots: SnapshotStore,
    report: ScanReport,
    gone: dict[str, FileInfo],
    new_files: list[FileInfo],
    now: str,
) -> None:
    """Files that disappeared: a move if a new file has the same bytes, else missing."""
    by_content: dict[str, FileInfo] = {}
    for f in gone.values():
        if f.content_sha256:
            by_content.setdefault(f.content_sha256, f)
    for new in new_files:
        old = by_content.pop(new.content_sha256 or "", None)
        if old is None or old.path not in gone:
            continue
        del gone[old.path]
        move_file(conn, old, new, now)
        snapshots.log_move(root.id, old.path, new.path, now)
        report.new.remove(new.path)
        report.moved.append((old.path, new.path))
    for path in sorted(gone):
        set_status(conn, root.id, path, "missing")
        report.missing.append(path)


def _sorted(report: ScanReport) -> ScanReport:
    report.new.sort()
    report.changed.sort()
    report.errors.sort()
    report.skipped.sort()
    report.slow = sorted(report.slow, key=lambda s: -s[1])[:10]
    return report


def scan_files(
    conn: sqlite3.Connection,
    root: Root,
    connector: Connector,
    snapshots: SnapshotStore,
    paths: Iterable[str],
    settings: Settings | None = None,
) -> ScanReport:
    """Scan only these files (root-relative POSIX paths), e.g. the ones a watcher saw change.

    Same rules as `scan_root`; a full scan still runs from time to time to catch anything a
    watcher missed."""
    describe = getattr(connector, "describe", None)
    is_ignored = getattr(connector, "is_ignored", None)
    if describe is None or is_ignored is None:
        return scan_root(conn, root, connector, snapshots, settings)
    settings = settings or Settings()
    report = ScanReport(root.id)
    started = time.perf_counter()
    now = utc_now()
    with conn:
        upsert_root(conn, root)
    gone: dict[str, FileInfo] = {}
    new_files: list[FileInfo] = []
    jobs: list[Job] = []
    for path in sorted(set(paths)):
        if is_ignored(path):
            continue
        old = get_file(conn, root.id, path)
        remote: RemoteFile | None = describe(path)
        if remote is None:
            if old is not None and old.status == "present":
                gone[path] = old
            continue
        if _unchanged(old, remote):
            report.unchanged += 1
        elif extractor_for(remote.path) is None:
            name_only = ExtractResult("", _stem(remote.path), "name_only")
            stored = _apply(conn, root, remote, old, Examined(None, name_only, False, False),
                            now, snapshots, report)  # fmt: skip
            if stored is not None:
                new_files.append(stored)
        else:
            if old is None or old.extract_status == PENDING:
                upsert_file(conn, _pending_row(root, remote, now), None)
                old = None
            jobs.append((remote, old))
    known = _texts_known(conn) if jobs else frozenset()
    try:
        for remote, old, examined in _examine_all(connector, jobs, settings, report, known):
            stored = _apply(conn, root, remote, old, examined, now, snapshots, report)
            if stored is not None:
                new_files.append(stored)
        _settle_gone(conn, root, snapshots, report, gone, new_files, now)
    finally:
        snapshots.flush()
        conn.commit()
    report.seconds = time.perf_counter() - started
    return _sorted(report)


def _texts_known(conn: sqlite3.Connection) -> frozenset[str]:
    """Content hashes of files whose text is already indexed, so copies aren't re-read."""
    rows = conn.execute(
        "SELECT DISTINCT content_sha256 FROM files WHERE content_sha256 IS NOT NULL "
        "AND extract_status IN ('ok', 'needs_ocr') LIMIT 1000000"
    )
    return frozenset(r[0] for r in rows)


def _copied_result(conn: sqlite3.Connection, content_sha: str, path: str) -> ExtractResult:
    """The indexed text of another file with these exact bytes."""
    row = conn.execute(
        "SELECT f.title, f.extract_status, t.body, f.path FROM files f "
        "JOIN files_fts t ON t.rowid = f.id WHERE f.content_sha256 = ? "
        "AND f.extract_status IN ('ok', 'needs_ocr') LIMIT 1",
        (content_sha,),
    ).fetchone()
    if row is None:  # gone since the scan started: an empty result is safe; next scan fixes
        return ExtractResult("", _stem(path), "name_only")
    title = row[0]
    if title == _stem(row[3]):  # a title taken from the other file's name, not its contents
        title = _stem(path)
    return ExtractResult(row[2] or "", title, row[1])


def _pending_row(root: Root, remote: RemoteFile, now: str) -> FileInfo:
    return FileInfo(
        root_id=root.id,
        path=remote.path,
        size=remote.size,
        modified=remote.modified,
        ext=extension(remote.path),
        placeholder=remote.is_placeholder,
        title=_stem(remote.path),
        extract_status=PENDING,
        last_seen=now,
    )


def _unchanged(old: FileInfo | None, remote: RemoteFile) -> bool:
    return (
        old is not None
        and old.status == "present"
        and old.extract_status != PENDING
        and old.size == remote.size
        and old.modified == remote.modified
        and old.placeholder == remote.is_placeholder
    )


def _apply(
    conn: sqlite3.Connection,
    root: Root,
    remote: RemoteFile,
    old: FileInfo | None,
    examined: Examined,
    now: str,
    snapshots: SnapshotStore,
    report: ScanReport,
) -> FileInfo | None:
    """Store what was found for one file. Returns the stored row if the file is new."""
    present = old is not None and old.status == "present"
    base = FileInfo(
        root_id=root.id,
        path=remote.path,
        size=remote.size,
        modified=remote.modified,
        ext=extension(remote.path),
        content_sha256=examined.content_sha256,
        placeholder=remote.is_placeholder,
        last_seen=now,
    )
    if examined.duplicate and examined.content_sha256 is not None:
        examined = replace(
            examined, result=_copied_result(conn, examined.content_sha256, remote.path)
        )
    if examined.result is None:
        # Touched or re-saved with identical bytes: update the metadata, keep the text.
        assert old is not None  # noqa: S101 - only files we had can have unchanged bytes
        upsert_file(conn, _keep_text(base, old), None)
        report.unchanged += 1
        return None

    result = examined.result
    if examined.extracted:
        report.extracted += 1
    if result.status == "error":
        report.errors.append((remote.path, result.reason or "unknown error"))
        log.info("Could not read %s: %s", remote.path, result.reason)
        if not examined.read and present:
            return None  # keep what we had; a read error is not a change
    elif not examined.read and result.status != "name_only":
        report.skipped.append((remote.path, result.reason or result.status))

    text_sha = sha256_text(result.text) if result.text else None
    info = replace(base, text_sha256=text_sha, title=result.title, extract_status=result.status)
    old_text = old.text_sha256 if old is not None else None
    if result.text and text_sha != old_text:
        snapshots.put(result.text)
        snapshots.log_version(root.id, remote.path, text_sha or "", now,
                              content_sha256=examined.content_sha256)  # fmt: skip
    stored = upsert_file(conn, info, result.text)

    if old is None:
        report.new.append(remote.path)
        return stored
    if not present:
        report.new.append(remote.path)  # it came back
    elif text_sha != old_text:
        report.changed.append(remote.path)
    else:
        report.unchanged += 1
    return None


def _keep_text(base: FileInfo, old: FileInfo) -> FileInfo:
    return replace(
        base, text_sha256=old.text_sha256, title=old.title, extract_status=old.extract_status
    )


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Root],
    connectors: dict[str, Connector],
    snapshots: SnapshotStore,
    settings: Settings | None = None,
    progress: Progress | None = None,
) -> list[ScanReport]:
    """Scan every root with search on."""
    return [
        scan_root(conn, root, connectors[root.id], snapshots, settings, progress)
        for root in roots
        if root.search
    ]
