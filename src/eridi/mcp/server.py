"""MCP server: lets Claude and other AI assistants search and read the lab's record.

Runs over stdio (`eridi mcp`). Every tool calls `eridi.core.service.Eridi`. Results are
JSON with file IDs, paths, dates, authors and entry IDs, so answers can cite sources.
Read-only unless the user sets `[mcp] allow_writes = true`; notes written through MCP are
marked `author.kind = "agent"`.

Output is capped (`MAX_OUTPUT_CHARS`): long text is shortened and long lists cut, with a
note saying so, so one large document can't flood an assistant's context.
"""

import functools
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from eridi import __version__
from eridi.core.config import ConfigError
from eridi.core.index.search import SearchHit, group_label
from eridi.core.models import Entry
from eridi.core.record.store import RecordFolderError
from eridi.core.service import Eridi, EridiError, file_id

MAX_OUTPUT_CHARS = 24_000
MAX_TEXT_CHARS = 4_000
TRUNCATED_NOTE = "Output was shortened to fit. Ask for less (a lower limit or one file)."

INSTRUCTIONS = """\
Eridi is this lab's research memory: its files (protocols, data, notes) and a record of
why things changed and whether they worked. Use `search` first, then `file_history` or
`get_entry` for details. Cite entry IDs, file paths, authors and dates in answers.
Entries with state "superseded" or "retracted" are kept for history but are not current.
Entries whose author kind is "agent" were written by an AI assistant."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def friendly[**P](func: Callable[P, str]) -> Callable[P, str]:
    """Pass Eridi's user-facing error messages to the assistant instead of a generic error."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return func(*args, **kwargs)
        except (EridiError, ConfigError, RecordFolderError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _shorten(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [shortened; {len(text) - limit} more characters]"


def fit(data: dict[str, Any], limit: int = MAX_OUTPUT_CHARS) -> str:
    """JSON for `data`, no longer than `limit`: shorten strings, then drop list items."""

    def shorten_all(value: Any, n: int) -> Any:
        if isinstance(value, str):
            return _shorten(value, n)
        if isinstance(value, list):
            return [shorten_all(v, n) for v in value]
        if isinstance(value, dict):
            return {k: shorten_all(v, n) for k, v in value.items()}
        return value

    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= limit:
        return text
    data = shorten_all(data, MAX_TEXT_CHARS)
    data["note"] = TRUNCATED_NOTE
    text = json.dumps(data, ensure_ascii=False)
    lists = [k for k, v in data.items() if isinstance(v, list)]
    while len(text) > limit and any(data[k] for k in lists):
        longest = max(lists, key=lambda k: len(json.dumps(data[k], ensure_ascii=False)))
        data[longest] = data[longest][:-1]
        text = json.dumps(data, ensure_ascii=False)
    if len(text) > limit:
        data = shorten_all(data, 500)
        text = json.dumps(data, ensure_ascii=False)
    return text[:limit]


def entry_json(eridi: Eridi, entry: Entry) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "created_at": entry.created_at,
        "author": {"name": entry.author.name, "kind": entry.author.kind},
        "state": eridi.entry_state(entry.id) or "current",
        "text": entry.text,
        "files": [
            {
                "file_id": f"{s.root_id}:{s.path}",
                "path": s.path,
                "storage": s.root_label or eridi.root_label(s.root_id),
            }
            for s in entry.subjects
        ],
    }
    if entry.outcome:
        out["outcome"] = entry.outcome
    if entry.tags:
        out["tags"] = list(entry.tags)
    if entry.change is not None and entry.change.summary:
        out["change"] = entry.change.summary
    if entry.project_id:
        out["project_id"] = entry.project_id
    if entry.project is not None:
        out["project"] = {"name": entry.project.name}
    if entry.supersedes:
        out["supersedes"] = entry.supersedes
    if entry.target_id:
        out["target_id"] = entry.target_id
    return out


def hit_json(hit: SearchHit) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "record entry" if hit.kind == "entry" else "file",
        "title": hit.title,
        "snippet": hit.snippet_with("[", "]"),
    }
    if hit.kind == "entry":
        out |= {"entry_id": hit.id, "kind": hit.entry_kind, "author": hit.author,
                "created_at": hit.created_at}  # fmt: skip
    else:
        out["file_id"] = hit.id
        if hit.stats is not None and hit.stats.interesting:
            out["history"] = {
                "versions": hit.stats.versions,
                "notes_about_it": hit.stats.notes,
                "last_edited": hit.stats.last_edited,
                "latest_change_unexplained": hit.stats.needs_why,
            }
    if hit.path:
        out |= {"path": hit.path, "storage": hit.root_label}
    out["match"] = group_label(hit).lower()
    return out


def build_server(eridi: Eridi) -> MCPServer:
    server: MCPServer = MCPServer(
        name="eridi", title="Eridi", version=__version__, instructions=INSTRUCTIONS
    )

    @server.tool(annotations=READ_ONLY)
    @friendly
    def search(query: str, limit: int = 10, project: str | None = None) -> str:
        """Search the lab's files and record together. Use quotes for an exact phrase.
        `project` (a name or ID from `list_projects`) limits it to that project."""
        hits = eridi.search(query, max(1, min(limit, 50)), project)
        return fit({"query": query, "results": [hit_json(h) for h in hits]})

    @server.tool(annotations=READ_ONLY)
    @friendly
    def file_history(file: str) -> str:
        """A file's text versions, moves, and every record entry about it, oldest first.
        `file` is a file_id, a path, or part of the file name."""
        h = eridi.file_history(file)
        return fit(
            {
                "file": {
                    "file_id": file_id(h.file),
                    "path": h.file.path,
                    "storage": h.root_label,
                    "status": h.file.status,
                    "title": h.file.title,
                },
                "versions": [
                    {
                        "text_sha256": v.text_sha256,
                        "first_seen": v.first_seen,
                        "current": v.is_current,
                        "diff_available": v.available,
                    }
                    for v in h.versions
                ],
                "moves": [{"from": f, "to": t, "at": at} for f, t, at in h.moves],
                "entries": [entry_json(eridi, i.entry) | {"state": i.state} for i in h.entries],
            }
        )

    @server.tool(annotations=READ_ONLY)
    @friendly
    def list_projects() -> str:
        """The lab's projects, each with the storage folders whose files belong to it.
        Other tools take a project's name or ID as `project`."""
        return fit(
            {
                "projects": [
                    {
                        "project_id": p.id,
                        "name": p.name,
                        "folders": [
                            {"storage": eridi.root_label(f.root_id), "path": f.path or "(all)"}
                            for f in p.folders
                        ],
                    }
                    for p in eridi.list_projects()
                ]
            }
        )

    @server.tool(annotations=READ_ONLY)
    @friendly
    def failures(file: str | None = None, project: str | None = None) -> str:
        """Everything recorded as failed: for one file (all its versions and earlier names),
        a project, or the whole lab. Each item is the note or decision, or a file version,
        and the outcome entry that judged it failed. Newest first."""
        found = eridi.failures(file=file, project=project)
        return fit({"failures": [
            {"entry": entry_json(eridi, f.entry), "judged_by": entry_json(eridi, f.verdict.entry),
             "path": f.path}
            for f in found
        ]})  # fmt: skip

    @server.tool(annotations=READ_ONLY)
    @friendly
    def open_questions(project: str | None = None, days: int | None = None) -> str:
        """Changes and decisions still without an outcome ("did it work?") after `days`
        (default: the lab's setting, usually 14), and file changes still without a why.
        Oldest first."""
        found = eridi.open_questions(project=project, days=days)
        return fit({"questions": [
            {"kind": q.kind, "since": q.since, "path": q.path,
             "entry": entry_json(eridi, q.entry) if q.entry else None,
             "summary": q.change.summary if q.change else None}
            for q in found
        ]})  # fmt: skip

    @server.tool(annotations=READ_ONLY)
    @friendly
    def get_entry(entry_id: str) -> str:
        """One record entry by its ID."""
        return fit(entry_json(eridi, eridi.get_entry(entry_id)))

    @server.tool(annotations=READ_ONLY)
    @friendly
    def list_recent(project: str | None = None, kind: str | None = None, limit: int = 20) -> str:
        """The newest current record entries, optionally for one project (name or ID).
        `kind` is note, decision, outcome, link or project."""
        entries = eridi.recent(max(1, min(limit, 100)), kind=kind, project=project)
        return fit({"entries": [entry_json(eridi, e) for e in entries]})

    @server.tool(annotations=READ_ONLY)
    @friendly
    def recent_activity(limit: int = 30) -> str:
        """What was edited or added recently across the lab's files, newest first, with a
        one-line summary of each edit and any note explaining it."""
        items = eridi.activity(max(1, min(limit, 200)))
        return fit(
            {
                "activity": [
                    {
                        "at": a.at,
                        "kind": a.kind,
                        "file_id": f"{a.root_id}:{a.path}",
                        "path": a.path,
                        "storage": a.root_label,
                        "summary": a.summary,
                        "notes": [
                            {"entry_id": n.entry_id, "text": n.text, "author": n.author}
                            for n in a.explanations
                        ],
                        "needs_why": a.needs_why,
                    }
                    for a in items
                ]
            }
        )

    @server.tool(annotations=READ_ONLY)
    @friendly
    def pending_changes(project: str | None = None) -> str:
        """Files whose text changed and that nobody has explained yet, optionally for one
        project (name or ID)."""
        changes = eridi.pending_changes(project)
        return fit(
            {
                "changes": [
                    {
                        "file_id": f"{c.root_id}:{c.path}",
                        "path": c.path,
                        "storage": c.root_label,
                        "summary": c.summary,
                        "detected_at": c.detected_at,
                    }
                    for c in changes
                ]
            }
        )

    @server.tool(annotations=READ_ONLY)
    @friendly
    def show_diff(file: str, from_version: str | None = None, to_version: str | None = None) -> str:
        """What changed in a file. By default its previous version against the current one;
        versions are `text_sha256` values from `file_history`."""
        info = eridi.resolve_file(file)
        d = eridi.diff(file_id(info), from_version, to_version)
        return fit(
            {
                "file_id": file_id(info),
                "path": info.path,
                "available": d.available,
                "summary": d.summary,
                "added_lines": d.added,
                "removed_lines": d.removed,
                "diff": d.unified,
            }
        )

    if eridi.config.mcp_allow_writes:

        @server.tool()
        @friendly
        def add_note(
            text: str,
            file: str | None = None,
            outcome: str | None = None,
            project: str | None = None,
        ) -> str:
            """Add a note to the lab's record, marked as written by an AI assistant. Only
            write what the user asked you to record. `outcome`: worked, partial, failed or
            unknown."""
            entry = eridi.add_note(
                text, file=file, outcome=outcome, project=project,
                author_kind="agent", agent_name="AI assistant",
            )  # fmt: skip
            return fit({"saved": entry_json(eridi, entry)})

    return server


def run_stdio() -> None:  # pragma: no cover - exercised by the human check
    with Eridi() as eridi:
        build_server(eridi).run("stdio")
