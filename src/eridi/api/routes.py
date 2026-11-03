"""JSON endpoints under /api. Each one is a thin wrapper over the Eridi service."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from eridi.api.scanner import BackgroundScanner
from eridi.api.security import Guard
from eridi.core.changes import FileDiff, PendingChange
from eridi.core.config import ConfigError
from eridi.core.index.search import SearchHit, group_label
from eridi.core.models import Entry, FileInfo, Root
from eridi.core.record.store import RecordFolderError
from eridi.core.service import (
    AmbiguousFileError,
    Eridi,
    EridiError,
    FileNotFoundInIndexError,
    UnknownEntryError,
    file_id,
)

router = APIRouter()


def _eridi(request: Request) -> Eridi:
    eridi: Eridi = request.app.state.eridi
    return eridi


def _scanner(request: Request) -> BackgroundScanner:
    scanner: BackgroundScanner = request.app.state.scanner
    return scanner


def error(exc: Exception) -> JSONResponse:
    if isinstance(exc, AmbiguousFileError):
        return JSONResponse(
            {"error": str(exc), "candidates": [file_json(f) for f in exc.candidates[:20]]},
            status_code=409,
        )
    status = 404 if isinstance(exc, FileNotFoundInIndexError | UnknownEntryError) else 400
    return JSONResponse({"error": str(exc)}, status_code=status)


KNOWN_ERRORS = (EridiError, ConfigError, RecordFolderError)


# --- JSON shapes ---


def file_json(f: FileInfo, label: str | None = None) -> dict[str, Any]:
    return {
        "id": file_id(f),
        "path": f.path,
        "name": f.path.rsplit("/", 1)[-1],
        "title": f.title,
        "status": f.status,
        "storage_id": f.root_id,
        "storage": label,
        "extract_status": f.extract_status,
    }


def entry_json(eridi: Eridi, e: Entry, state: str | None = None) -> dict[str, Any]:
    return {
        "id": e.id,
        "kind": e.kind,
        "created_at": e.created_at,
        "author": e.author.name,
        "author_kind": e.author.kind,
        "text": e.text,
        "outcome": e.outcome,
        "tags": list(e.tags),
        "change": e.change.summary if e.change else None,
        "files": [
            {
                "id": f"{s.root_id}:{s.path}",
                "path": s.path,
                "storage": s.root_label or eridi.root_label(s.root_id),
            }
            for s in e.subjects
        ],
        "target_id": e.target_id,
        "supersedes": e.supersedes,
        "state": state or eridi.entry_state(e.id) or "current",
    }


def hit_json(h: SearchHit) -> dict[str, Any]:
    return {
        "type": h.kind,
        "id": h.id,
        "title": h.title,
        "snippet": h.segments(),
        "path": h.path,
        "file_id": f"{h.root_id}:{h.path}" if h.root_id and h.path else None,
        "storage": h.root_label,
        "kind": h.entry_kind,
        "author": h.author,
        "created_at": h.created_at,
        "partial": not h.matched_all,
        "match": h.match,
        "group": group_label(h),
    }


def change_json(c: PendingChange) -> dict[str, Any]:
    return {
        "key": c.key,
        "file_id": f"{c.root_id}:{c.path}",
        "path": c.path,
        "title": c.title,
        "storage": c.root_label,
        "summary": c.summary,
        "detected_at": c.detected_at,
    }


def diff_json(d: FileDiff) -> dict[str, Any]:
    return {
        "available": d.available,
        "summary": d.summary,
        "added": d.added,
        "removed": d.removed,
        "lines": [{"op": ln.op, "text": ln.text} for ln in d.lines],
    }


def root_json(r: Root) -> dict[str, Any]:
    return {"id": r.id, "label": r.label, "location": r.location, "kind": r.kind,
            "search": r.search, "save": r.save}  # fmt: skip


# --- request bodies ---


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    file: str | None = None
    outcome: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class OutcomeIn(BaseModel):
    outcome: str
    entry_id: str | None = None
    file: str | None = None
    text: str = Field(default="", max_length=20_000)


class DismissIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


class StorageIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    search: bool | None = None
    save: bool | None = None


# --- endpoints ---


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    eridi = _eridi(request)
    scan = _scanner(request).status
    # While a scan runs it holds the index; answer at once instead of waiting for it.
    pending = None if scan.running else len(eridi.pending_changes())
    return {
        "initialized": eridi.initialized,
        "user": eridi.config.user.name or None,
        "storage_count": len(eridi.list_roots()),
        "pending_count": pending,
        "scan": {
            "running": scan.running,
            "last_started": scan.last_started,
            "last_finished": scan.last_finished,
            "last_error": scan.last_error,
            "last_summary": scan.last_summary,
        },
    }


@router.get("/search")
def search(request: Request, q: str = "", limit: int = 30) -> Any:
    try:
        hits = _eridi(request).search(q, max(1, min(limit, 100)))
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {"query": q, "results": [hit_json(h) for h in hits]}


@router.get("/changes")
def changes(request: Request) -> Any:
    try:
        return {"changes": [change_json(c) for c in _eridi(request).pending_changes()]}
    except KNOWN_ERRORS as exc:
        return error(exc)


@router.post("/changes/dismiss")
def dismiss(request: Request, body: DismissIn) -> Any:
    _eridi(request).dismiss(body.key)
    return {"dismissed": body.key}


@router.get("/diff")
def diff(
    request: Request,
    file: str,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
) -> Any:
    try:
        return diff_json(_eridi(request).diff(file, from_, to))
    except KNOWN_ERRORS as exc:
        return error(exc)


@router.post("/notes")
def add_note(request: Request, body: NoteIn) -> Any:
    eridi = _eridi(request)
    try:
        entry = eridi.add_note(body.text, file=body.file, outcome=body.outcome or None,
                               tags=body.tags)  # fmt: skip
    except KNOWN_ERRORS as exc:
        return error(exc)
    return JSONResponse(entry_json(eridi, entry), status_code=201)


@router.post("/outcomes")
def add_outcome(request: Request, body: OutcomeIn) -> Any:
    eridi = _eridi(request)
    try:
        entry = eridi.add_outcome(body.outcome, entry_id=body.entry_id, file=body.file,
                                  text=body.text)  # fmt: skip
    except KNOWN_ERRORS as exc:
        return error(exc)
    return JSONResponse(entry_json(eridi, entry), status_code=201)


@router.get("/history")
def history(request: Request, file: str) -> Any:
    eridi = _eridi(request)
    try:
        h = eridi.file_history(file)
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {
        "file": file_json(h.file, h.root_label),
        "versions": [
            {
                "text_sha256": v.text_sha256,
                "first_seen": v.first_seen,
                "current": v.is_current,
                "available": v.available,
            }
            for v in h.versions
        ],
        "moves": [{"from": f, "to": t, "at": at} for f, t, at in h.moves],
        "entries": [entry_json(eridi, i.entry, i.state) for i in h.entries],
    }


@router.get("/recent")
def recent(request: Request, limit: int = 20) -> Any:
    eridi = _eridi(request)
    try:
        entries = eridi.recent(max(1, min(limit, 200)))
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {"entries": [entry_json(eridi, e, "current") for e in entries]}


@router.get("/files")
def files(request: Request, q: str = "", limit: int = 20) -> Any:
    eridi = _eridi(request)
    found = eridi.find_files(q, max(1, min(limit, 100))) if q.strip() else []
    return {"files": [file_json(f, eridi.root_label(f.root_id)) for f in found]}


@router.get("/storage")
def storage(request: Request) -> Any:
    return {"storage": [root_json(r) for r in _eridi(request).list_roots()]}


@router.patch("/storage/{root_id}")
def update_storage(request: Request, root_id: str, body: StorageIn) -> Any:
    try:
        root = _eridi(request).update_root(
            root_id, label=body.label, search=body.search, save=body.save
        )
    except KNOWN_ERRORS as exc:
        return error(exc)
    return root_json(root)


@router.post("/scan")
def scan(request: Request, wait: bool = False) -> Any:
    scanner = _scanner(request)
    if not _eridi(request).list_roots():
        return error(EridiError("No storage connected. Add a folder with: eridi storage add"))
    if wait:
        result = scanner.run_now()
        return {"started": True, "finished": True, "summary": result.last_summary,
                "error": result.last_error}  # fmt: skip
    started = scanner.request()
    return JSONResponse({"started": started, "finished": False}, status_code=202)


@router.post("/session-link")
def session_link(request: Request) -> Any:
    """A one-time sign-in link for the browser. Needs the bearer token (used by `eridi open`)."""
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return JSONResponse({"error": "Use the API token for this."}, status_code=403)
    guard: Guard = request.app.state.guard
    return {"url": f"http://127.0.0.1:{guard.port}/?t={guard.new_link()}"}
