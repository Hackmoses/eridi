"""Detected changes waiting for a "why", and readable diffs between text versions.

Pending changes are derived, never stored: a file's current text is a pending change when
it has an earlier version, no record entry explains it, and the user hasn't dismissed it.
An entry explains it when its `change.to_text_sha256`, or one of its subjects'
`text_sha256`, equals the file's current text hash.
"""

import difflib
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Literal

from eridi.core.config import Config
from eridi.core.index.files import text_versions
from eridi.core.models import FileInfo
from eridi.core.snapshots import SnapshotStore

SIDE_LIMIT = 60
CONTEXT_LINES = 3


@dataclass(frozen=True)
class PendingChange:
    file_id: int
    root_id: str
    path: str
    title: str | None
    from_text_sha256: str
    to_text_sha256: str
    detected_at: str
    summary: str | None
    root_label: str | None = None

    @property
    def key(self) -> str:
        """Stable identity for dismissing; survives the file being renamed."""
        return change_key(self.root_id, self.from_text_sha256, self.to_text_sha256)


def change_key(root_id: str, from_sha: str, to_sha: str) -> str:
    return f"{root_id}:{from_sha[:16]}:{to_sha[:16]}"


@dataclass(frozen=True)
class DiffLine:
    op: Literal[" ", "-", "+", "@"]
    """Unchanged, removed, added, or "@" for a gap between groups of changes."""
    text: str


@dataclass(frozen=True)
class FileDiff:
    from_text_sha256: str | None
    to_text_sha256: str
    lines: tuple[DiffLine, ...]
    summary: str | None
    available: bool = True
    """False when a version's text is no longer stored (evicted, or never snapshotted)."""
    added: int = 0
    removed: int = 0

    @property
    def unified(self) -> str:
        """The diff as plain text, one line per row with a `-`, `+` or space prefix."""
        return "\n".join("…" if ln.op == "@" else f"{ln.op} {ln.text}" for ln in self.lines)


def _clip(text: str) -> str:
    return text if len(text) <= SIDE_LIMIT else text[: SIDE_LIMIT - 1] + "…"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split(" | ")]


def summarize_pair(old: str, new: str) -> str:
    """One readable line for a changed line, e.g. `Incubation: 1 h at RT → 2 h at RT`."""
    old, new = old.strip().rstrip("."), new.strip().rstrip(".")
    if " | " in old and " | " in new:
        a, b = _cells(old), _cells(new)
        if len(a) == len(b) and a[0] == b[0] and len(a) > 1:
            changed = [i for i in range(1, len(a)) if a[i] != b[i]]
            if changed:
                before = " | ".join(a[i] for i in changed)
                after = " | ".join(b[i] for i in changed)
                return f"{_clip(a[0])}: {_clip(before)} → {_clip(after)}"
    label_a, sep_a, rest_a = old.partition(": ")
    label_b, sep_b, rest_b = new.partition(": ")
    if sep_a and sep_b and label_a == label_b and rest_a != rest_b:
        return f"{_clip(label_a)}: {_clip(rest_a)} → {_clip(rest_b)}"
    return f"{_clip(old)} → {_clip(new)}"


def summarize(old_lines: list[str], new_lines: list[str]) -> str | None:
    """A one-line summary of the first change between two texts."""
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, _i2, j1, _j2 in matcher.get_opcodes():
        if tag == "replace":
            return summarize_pair(old_lines[i1], new_lines[j1])
        if tag == "delete":
            return f"Removed: {_clip(old_lines[i1].strip())}"
        if tag == "insert":
            return f"Added: {_clip(new_lines[j1].strip())}"
    return None


def diff_texts(
    old: str, new: str, *, context: int = CONTEXT_LINES
) -> tuple[list[DiffLine], str | None]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    lines: list[DiffLine] = []
    for n, group in enumerate(matcher.get_grouped_opcodes(context)):
        if n > 0:
            lines.append(DiffLine("@", ""))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [DiffLine(" ", t) for t in old_lines[i1:i2]]
                continue
            lines += [DiffLine("-", t) for t in old_lines[i1:i2]]
            lines += [DiffLine("+", t) for t in new_lines[j1:j2]]
    return lines, summarize(old_lines, new_lines)


def diff(snapshots: SnapshotStore, from_sha: str | None, to_sha: str) -> FileDiff:
    """Diff two stored text versions. `from_sha=None` diffs against empty text."""
    new = snapshots.get(to_sha)
    old = "" if from_sha is None else snapshots.get(from_sha)
    if new is None or old is None:
        return FileDiff(from_sha, to_sha, (), None, available=False)
    lines, summary = diff_texts(old, new)
    return FileDiff(
        from_sha,
        to_sha,
        tuple(lines),
        summary,
        added=sum(1 for ln in lines if ln.op == "+"),
        removed=sum(1 for ln in lines if ln.op == "-"),
    )


def previous_version(conn: sqlite3.Connection, file_id: int, current: str) -> str | None:
    """The most recent version before `current`, by when it was first seen."""
    others = [sha for sha, _, _ in text_versions(conn, file_id) if sha != current]
    return others[-1] if others else None


def file_diff(
    conn: sqlite3.Connection,
    snapshots: SnapshotStore,
    file: FileInfo,
    from_sha: str | None = None,
    to_sha: str | None = None,
) -> FileDiff:
    """Diff a file's versions; by default its previous version against its current one."""
    if file.id is None:
        raise ValueError("The file must come from the index.")
    to_sha = to_sha or file.text_sha256
    if to_sha is None:
        return FileDiff(from_sha, "", (), None, available=False)
    if from_sha is None:
        from_sha = previous_version(conn, file.id, to_sha)
    return diff(snapshots, from_sha, to_sha)


def pending_changes(
    conn: sqlite3.Connection,
    snapshots: SnapshotStore,
    dismissed: Collection[str] = (),
    project: str | None = None,
) -> list[PendingChange]:
    """Text changes nobody has explained or dismissed yet, newest first."""
    project_filter = ""
    params: list[object] = []
    if project is not None:
        project_filter = (
            "AND EXISTS (SELECT 1 FROM project_roots pr WHERE pr.project_id = ? "
            "AND pr.root_id = f.root_id AND (pr.path_prefix = '' OR f.path = pr.path_prefix "
            "OR substr(f.path, 1, length(pr.path_prefix) + 1) = pr.path_prefix || '/'))"
        )
        params.append(project)
    rows = conn.execute(
        f"""
        SELECT f.id, f.root_id, f.path, f.title, f.text_sha256, r.label AS root_label,
               v.first_seen
        FROM files f
        JOIN text_versions v ON v.file_id = f.id AND v.text_sha256 = f.text_sha256
        LEFT JOIN roots r ON r.id = f.root_id
        WHERE f.status = 'present' AND f.text_sha256 IS NOT NULL
          AND (SELECT count(*) FROM text_versions o WHERE o.file_id = f.id) > 1
          AND NOT EXISTS (
            SELECT 1 FROM entries e
            WHERE e.state != 'retracted' AND e.kind != 'tombstone' AND (
              e.change_to_text_sha256 = f.text_sha256
              OR e.id IN (SELECT s.entry_id FROM entry_subjects s
                          WHERE s.text_sha256 = f.text_sha256)))
          {project_filter}
        ORDER BY v.first_seen DESC, f.path
        """,  # noqa: S608 - only constant SQL fragments are interpolated
        params,
    ).fetchall()
    dismissed = set(dismissed)
    out: list[PendingChange] = []
    for r in rows:
        from_sha = previous_version(conn, r["id"], r["text_sha256"])
        if from_sha is None or change_key(r["root_id"], from_sha, r["text_sha256"]) in dismissed:
            continue
        out.append(
            PendingChange(
                file_id=r["id"],
                root_id=r["root_id"],
                path=r["path"],
                title=r["title"],
                from_text_sha256=from_sha,
                to_text_sha256=r["text_sha256"],
                detected_at=r["first_seen"],
                summary=diff(snapshots, from_sha, r["text_sha256"]).summary,
                root_label=r["root_label"],
            )
        )
    return out


def dismiss(config: Config, change: PendingChange) -> Config:
    """A copy of the config with the change dismissed. Save it to make it stick."""
    if change.key in config.dismissed_changes:
        return config
    return replace(config, dismissed_changes=(*config.dismissed_changes, change.key))


BASELINE_PREFIX = "activity_baseline:"
"""Index meta, per root: versions first seen up to this time (the root's first complete
scan) were already there, so they aren't listed as "added"."""


@dataclass(frozen=True)
class Explanation:
    entry_id: str
    text: str
    author: str
    outcome: str | None


@dataclass(frozen=True)
class Activity:
    """One thing that happened to a file: a new text version (edited) or a new file (added)."""

    kind: Literal["edited", "added"]
    at: str
    file_id: int
    root_id: str
    path: str
    title: str | None
    root_label: str | None
    status: str
    from_text_sha256: str | None
    to_text_sha256: str
    summary: str | None
    is_current: bool
    explanations: tuple[Explanation, ...] = ()
    needs_why: bool = False


def recent_activity(
    conn: sqlite3.Connection,
    snapshots: SnapshotStore,
    dismissed: Collection[str] = (),
    limit: int = 50,
    since: str | None = None,
) -> list[Activity]:
    """What changed recently across all storage, newest first."""
    rows = conn.execute(
        """
        WITH v AS (
            SELECT file_id, text_sha256, first_seen,
                   lag(text_sha256) OVER (
                       PARTITION BY file_id ORDER BY first_seen, rowid) AS previous
            FROM text_versions
        )
        SELECT v.file_id, v.text_sha256, v.first_seen, v.previous,
               f.root_id, f.path, f.title, f.status, f.text_sha256 AS current_sha,
               r.label AS root_label
        FROM v
        JOIN files f ON f.id = v.file_id
        LEFT JOIN roots r ON r.id = f.root_id
        LEFT JOIN meta b ON b.key = ? || f.root_id
        WHERE (v.previous IS NOT NULL OR v.first_seen > coalesce(b.value, '9999'))
          AND (? IS NULL OR v.first_seen >= ?)
        ORDER BY v.first_seen DESC, f.path
        LIMIT ?
        """,
        (BASELINE_PREFIX, since, since, limit),
    ).fetchall()
    dismissed = set(dismissed)
    out: list[Activity] = []
    for r in rows:
        sha = r["text_sha256"]
        explanations = tuple(
            Explanation(e[0], e[1], e[2], e[3])
            for e in conn.execute(
                "SELECT DISTINCT e.id, e.text, e.author_name, e.outcome FROM entries e "
                "LEFT JOIN entry_subjects s ON s.entry_id = e.id "
                "WHERE e.state = 'current' AND e.kind != 'tombstone' "
                "AND (e.change_to_text_sha256 = ? OR s.text_sha256 = ?) ORDER BY e.id",
                (sha, sha),
            )
        )
        previous = r["previous"]
        is_current = r["current_sha"] == sha
        kind: Literal["edited", "added"] = "edited" if previous else "added"
        summary = diff(snapshots, previous, sha).summary if previous else None
        needs_why = (
            kind == "edited"
            and is_current
            and r["status"] == "present"
            and not explanations
            and change_key(r["root_id"], previous, sha) not in dismissed
        )
        out.append(
            Activity(
                kind=kind,
                at=r["first_seen"],
                file_id=r["file_id"],
                root_id=r["root_id"],
                path=r["path"],
                title=r["title"],
                root_label=r["root_label"],
                status=r["status"],
                from_text_sha256=previous,
                to_text_sha256=sha,
                summary=summary,
                is_current=is_current,
                explanations=explanations,
                needs_why=needs_why,
            )
        )
    return out
