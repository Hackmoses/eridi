"""A project's record as Markdown (for lab meetings and reports) or CSV (for spreadsheets).

Pure functions: same entries in, same text out. Only current entries are exported
(corrections replace what they corrected; retracted entries are left out), oldest first.
"""

import csv
import io
from collections.abc import Callable
from datetime import datetime

from eridi.core.models import DATA_LABELS, Entry, Project, parse_timestamp
from eridi.core.outcomes import Outcomes

CSV_COLUMNS = (
    "id", "created_at", "kind", "author", "author_kind", "storage", "file", "change", "text",
    "outcome", "result", "about_entry", "tags",
)  # fmt: skip
"""`outcome` is what the entry itself says; `result` is the current outcome, which later
outcome entries may have updated."""

KIND_NAMES = {"note": "Note", "decision": "Decision", "outcome": "Outcome", "link": "Link"}


def _exported(entries: list[Entry]) -> list[Entry]:
    return sorted(
        (e for e in entries if e.kind not in ("project", "tombstone")), key=lambda e: e.id
    )


def _stamp(value: str) -> str:
    return parse_timestamp(value).strftime("%Y-%m-%d %H:%M UTC")


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def export_markdown(
    project: Project,
    entries: list[Entry],
    outcomes: Outcomes,
    root_label: Callable[[str], str | None],
    *,
    now: datetime,
    period_label: str | None = None,
) -> str:
    chosen = _exported(entries)
    title = f"# {project.name}"
    facts = [f"Exported {now:%Y-%m-%d}", f"{len(chosen)} entries"]
    if period_label:
        facts.insert(1, period_label)
    facts += [DATA_LABELS.get(x, x) for x in project.labels]
    lines = [title, "", " · ".join(facts), ""]
    if not chosen:
        lines += ["Nothing recorded yet.", ""]
    by_id = {e.id: e for e in entries}
    month = None
    for e in chosen:
        moment = parse_timestamp(e.created_at)
        heading = moment.strftime("%B %Y")
        if heading != month:
            lines += [f"## {heading}", ""]
            month = heading
        result = outcomes.outcome_of(e) if e.kind != "outcome" else e.outcome
        who = e.author.name + (" (AI)" if e.author.kind == "agent" else "")
        head = [_stamp(e.created_at), KIND_NAMES.get(e.kind, e.kind), who]
        if result:
            head.append(result)
        lines += ["### " + " · ".join(head), ""]
        for s in e.subjects:
            storage = s.root_label or root_label(s.root_id) or s.root_id
            lines.append(f"**{s.path}** ({storage})  ")
        if e.subjects:
            lines[-1] = lines[-1].rstrip()
            lines.append("")
        if e.change is not None and e.change.summary:
            lines += [_quote(e.change.summary), ""]
        if e.kind == "outcome" and e.target_id:
            target = by_id.get(e.target_id)
            about = target.text.splitlines()[0] if target and target.text else e.target_id
            lines += [f"For: {about}", ""]
        if e.text:
            lines += [e.text.strip(), ""]
        if e.tags:
            lines += ["Tags: " + ", ".join(e.tags), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def export_csv(
    entries: list[Entry], outcomes: Outcomes, root_label: Callable[[str], str | None]
) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for e in _exported(entries):
        subject = e.subjects[0] if e.subjects else None
        storage = (subject.root_label or root_label(subject.root_id) or "") if subject else ""
        writer.writerow(
            [
                e.id,
                e.created_at,
                e.kind,
                e.author.name,
                e.author.kind,
                storage,
                "; ".join(s.path for s in e.subjects),
                e.change.summary if e.change and e.change.summary else "",
                e.text,
                e.outcome or "",
                (outcomes.outcome_of(e) if e.kind != "outcome" else e.outcome) or "",
                e.target_id or "",
                ", ".join(e.tags),
            ]
        )
    return out.getvalue()
