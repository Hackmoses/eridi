"""Did it work? The current outcome of each entry and each file version.

An outcome comes from, newest first:
1. an `outcome` entry that targets the entry (`target_id`), or targets the file version the
   entry explains (a subject with that `text_sha256`);
2. the entry's own `outcome` field (a note saved with "worked").

A file version's outcome is the newest `outcome` entry about that version, else the
outcome of the newest note that explains it. Only current entries count: a corrected or
retracted outcome no longer applies.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from eridi.core.models import Entry

OPEN = (None, "unknown")
"""Outcomes that leave the question open."""


@dataclass(frozen=True)
class Verdict:
    outcome: str
    entry: Entry
    """The entry that says so: an `outcome` entry, or the note itself."""


def explained_versions(entry: Entry) -> set[str]:
    """Text versions an entry is about: the change it explains, and its subjects' versions."""
    shas = {s.text_sha256 for s in entry.subjects if s.text_sha256}
    if entry.change is not None:
        shas.add(entry.change.to_text_sha256)
    return shas


class Outcomes:
    """Built once from current entries; answers "what's the outcome of X" quickly."""

    def __init__(self, current: Iterable[Entry]) -> None:
        self.by_entry: dict[str, Verdict] = {}
        self.by_version: dict[str, Verdict] = {}
        explained_by: dict[str, Verdict] = {}
        entries = sorted(current, key=lambda e: e.id)  # oldest first: newer ones win
        by_id = {e.id: e for e in entries}
        for e in entries:
            if e.kind == "outcome" and e.outcome:
                verdict = Verdict(e.outcome, e)
                shas = {s.text_sha256 for s in e.subjects if s.text_sha256}
                if e.target_id:
                    self.by_entry[e.target_id] = verdict
                    target = by_id.get(e.target_id)
                    if target is not None:  # the version that entry explains, too
                        shas |= explained_versions(target)
                for sha in shas:
                    self.by_version[sha] = verdict
            elif e.kind in ("note", "decision") and e.outcome:
                for sha in explained_versions(e):
                    explained_by[sha] = Verdict(e.outcome, e)
        for sha, verdict in explained_by.items():
            self.by_version.setdefault(sha, verdict)
        self._direct = dict(self.by_entry)

    def for_entry(self, entry: Entry) -> Verdict | None:
        """The newest outcome given for this entry or for the version it explains."""
        candidates = [v for v in (self._direct.get(entry.id),) if v is not None]
        for sha in explained_versions(entry):
            found = self.by_version.get(sha)
            if found is not None and found.entry.kind == "outcome":
                candidates.append(found)
        if candidates:
            return max(candidates, key=lambda v: v.entry.id)
        if entry.outcome:
            return Verdict(entry.outcome, entry)
        return None

    def outcome_of(self, entry: Entry) -> str | None:
        verdict = self.for_entry(entry)
        return verdict.outcome if verdict else None

    def for_version(self, text_sha256: str) -> Verdict | None:
        return self.by_version.get(text_sha256)
