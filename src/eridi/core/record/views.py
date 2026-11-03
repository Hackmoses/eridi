"""Views over the record: the current state, and the full history of a file.

Rules (see docs/RECORD_FORMAT.md):
- An entry that `supersedes` an earlier one replaces it. When several entries supersede the
  same one, the newest wins and the others count as superseded too.
- A `tombstone` retracts its `target_id`. A retracted tombstone no longer retracts anything,
  so retracting a retraction restores the entry.
- A retracted entry still supersedes what it replaced: deleting a corrected note removes it,
  rather than bringing back the version it corrected.
- References only count when they point to an older entry (a lower ID). That is always true
  for entries written by Eridi, and it rules out cycles.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import Literal

from eridi.core.models import Entry

EntryState = Literal["current", "superseded", "retracted"]


@dataclass(frozen=True)
class HistoryItem:
    entry: Entry
    state: EntryState
    superseded_by: str | None = None
    retracted_by: str | None = None


def resolve(entries: Iterable[Entry]) -> dict[str, HistoryItem]:
    """The state of every entry, keyed by ID."""
    by_id = {e.id: e for e in entries}

    tombstones_for: dict[str, list[str]] = {}
    superseders_for: dict[str, list[str]] = {}
    for e in by_id.values():
        if e.kind == "tombstone" and e.target_id is not None and e.target_id < e.id:
            tombstones_for.setdefault(e.target_id, []).append(e.id)
        if e.supersedes is not None and e.supersedes < e.id:
            superseders_for.setdefault(e.supersedes, []).append(e.id)

    @cache
    def retracted_by(entry_id: str) -> str | None:
        # Newest active tombstone wins. Recursion only goes to newer IDs, so it ends.
        for tomb in sorted(tombstones_for.get(entry_id, ()), reverse=True):
            if retracted_by(tomb) is None:
                return tomb
        return None

    winner_of: dict[str, str] = {}
    for target, superseders in superseders_for.items():
        winner = max(superseders)
        winner_of[target] = winner
        for loser in superseders:
            if loser != winner:
                winner_of.setdefault(loser, winner)

    items: dict[str, HistoryItem] = {}
    for entry_id in sorted(by_id):
        tomb = retracted_by(entry_id)
        sup = winner_of.get(entry_id)
        state: EntryState = "retracted" if tomb else "superseded" if sup else "current"
        items[entry_id] = HistoryItem(by_id[entry_id], state, sup, tomb)
    return items


def current(entries: Iterable[Entry]) -> list[Entry]:
    """Entries as they stand now: no superseded or retracted entries, and no tombstones."""
    return [
        item.entry
        for item in resolve(entries).values()
        if item.state == "current" and item.entry.kind != "tombstone"
    ]


def _about(entry: Entry, root_id: str, path: str) -> bool:
    if any(s.root_id == root_id and s.path == path for s in entry.subjects):
        return True
    for link in entry.links or ():
        for end in (link.from_, link.to):
            if isinstance(end, dict) and end.get("root_id") == root_id and end.get("path") == path:
                return True
    return False


def history(entries: Iterable[Entry], root_id: str, path: str) -> list[HistoryItem]:
    """Everything about one file, oldest first, with superseded and retracted entries labeled.

    Includes entries that mention the file, plus the entries that correct, retract or
    evaluate them, and the earlier versions they replaced.
    """
    items = resolve(entries)
    chosen = {i for i, item in items.items() if _about(item.entry, root_id, path)}
    while True:
        added: set[str] = set()
        for entry_id, item in items.items():
            e = item.entry
            if entry_id in chosen:
                added.update(r for r in (e.supersedes, e.target_id) if r is not None and r in items)
            elif e.target_id in chosen or e.supersedes in chosen:
                added.add(entry_id)
        added -= chosen
        if not added:
            break
        chosen |= added
    return [items[i] for i in sorted(chosen)]
