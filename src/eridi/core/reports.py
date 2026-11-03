"""The "did it work?" views (M13): what changed in a project over a period, everything that
failed, and open questions (changes still without an outcome after N days).

Data shapes and the pure parts live here; `Eridi` in `service.py` gathers the inputs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from eridi.core.changes import Activity, PendingChange
from eridi.core.models import Entry, Project, format_timestamp, parse_timestamp
from eridi.core.outcomes import OPEN, Outcomes, Verdict


@dataclass(frozen=True)
class Period:
    since: str
    until: str
    """UTC record timestamps; `since` inclusive, `until` exclusive."""
    label: str


def month_period(month: str | None = None, now: datetime | None = None) -> Period:
    """A calendar month in UTC, `YYYY-MM` (default: this month)."""
    now = now or datetime.now(UTC)
    if month is None:
        year, number = now.year, now.month
    else:
        try:
            year, number = (int(x) for x in month.split("-"))
            datetime(year, number, 1, tzinfo=UTC)
        except ValueError:
            raise ValueError(
                f"{month!r} isn't a month. Write it as YYYY-MM, e.g. 2026-11."
            ) from None
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=UTC)
    return Period(format_timestamp(start), format_timestamp(end), start.strftime("%B %Y"))


def days_period(since: str, now: datetime | None = None) -> Period:
    """From a date (`YYYY-MM-DD`) until now."""
    try:
        start = datetime.fromisoformat(since).replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(f"{since!r} isn't a date. Write it as YYYY-MM-DD.") from None
    end = (now or datetime.now(UTC)) + timedelta(seconds=1)
    return Period(format_timestamp(start), format_timestamp(end), f"since {start:%Y-%m-%d}")


@dataclass(frozen=True)
class ProjectPeriod:
    """What changed in a project over a period: file edits and new files, and what the
    record says (notes, decisions, outcomes), each with its current outcome."""

    project: Project
    period: Period
    changes: list[Activity]
    entries: list[Entry]
    results: dict[str, str | None]
    """Entry ID → current outcome, for `entries` and the changes' explanations."""

    @property
    def counts(self) -> dict[str, int]:
        results = [self.results.get(e.id) for e in self.entries if e.kind != "outcome"]
        return {
            "edited": sum(1 for a in self.changes if a.kind == "edited"),
            "added": sum(1 for a in self.changes if a.kind == "added"),
            "needs_why": sum(1 for a in self.changes if a.needs_why),
            "notes": sum(1 for e in self.entries if e.kind in ("note", "decision")),
            "worked": results.count("worked"),
            "partial": results.count("partial"),
            "failed": results.count("failed"),
        }


@dataclass(frozen=True)
class Failure:
    """Something that didn't work: a note or decision, or a file version, judged failed."""

    entry: Entry
    """The note or decision, or (for a version) the outcome entry about it."""
    verdict: Verdict
    path: str | None
    root_id: str | None


def failures(entries: list[Entry], outcomes: Outcomes) -> list[Failure]:
    """Failed notes and decisions, plus failed versions no note explains; newest first."""
    out: list[Failure] = []
    explained_ids = set()
    for e in entries:
        if e.kind not in ("note", "decision"):
            continue
        verdict = outcomes.for_entry(e)
        if verdict is not None and verdict.outcome == "failed":
            subject = e.subjects[0] if e.subjects else None
            out.append(Failure(e, verdict, subject.path if subject else None,
                               subject.root_id if subject else None))  # fmt: skip
            explained_ids.add(e.id)
            explained_ids.add(verdict.entry.id)
    for e in entries:
        if e.kind != "outcome" or e.outcome != "failed" or e.id in explained_ids:
            continue
        if e.target_id is not None:
            continue  # about an entry: listed with it if it still counts
        subject = e.subjects[0] if e.subjects else None
        out.append(Failure(e, Verdict("failed", e), subject.path if subject else None,
                           subject.root_id if subject else None))  # fmt: skip
    return sorted(out, key=lambda f: f.verdict.entry.id, reverse=True)


@dataclass(frozen=True)
class OpenQuestion:
    """A change or decision still waiting for "did it work?" after the chosen days."""

    kind: Literal["no_outcome", "no_why"]
    since: str
    """When the change was explained (or the decision made), or first seen if unexplained."""
    entry: Entry | None = None
    change: PendingChange | None = None

    @property
    def path(self) -> str | None:
        if self.change is not None:
            return self.change.path
        if self.entry is not None and self.entry.subjects:
            return self.entry.subjects[0].path
        return None


def open_questions(
    entries: list[Entry],
    pending: list[PendingChange],
    outcomes: Outcomes,
    days: int,
    now: datetime | None = None,
) -> list[OpenQuestion]:
    """Explained changes and decisions with no outcome yet, and changes with no why, that
    are at least `days` old; oldest first."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    out: list[OpenQuestion] = []
    for e in entries:
        is_change = e.kind == "note" and e.change is not None
        if not (is_change or e.kind == "decision"):
            continue
        if parse_timestamp(e.created_at) > cutoff or outcomes.outcome_of(e) not in OPEN:
            continue
        out.append(OpenQuestion("no_outcome", e.created_at, entry=e))
    for change in pending:
        if parse_timestamp(change.detected_at) <= cutoff:
            out.append(OpenQuestion("no_why", change.detected_at, change=change))
    return sorted(out, key=lambda q: q.since)
