"""JSON endpoints under /api. Each one is a thin wrapper over the Eridi service."""

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from eridi.api.scanner import BackgroundScanner
from eridi.api.security import Guard
from eridi.core.changes import Activity, FileDiff, PendingChange
from eridi.core.config import ConfigError
from eridi.core.index.search import SearchHit, group_label
from eridi.core.models import Entry, FileInfo, Project, Root
from eridi.core.record.store import RecordFolderError
from eridi.core.service import (
    AmbiguousFileError,
    Eridi,
    EridiError,
    FileNotFoundInIndexError,
    UnknownEntryError,
    UnknownProjectError,
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
    status = (
        404
        if isinstance(exc, FileNotFoundInIndexError | UnknownEntryError | UnknownProjectError)
        else 400
    )
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
        "project_id": e.project_id,
        "project_name": e.project.name if e.project else None,
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
        "history": None
        if h.stats is None
        else {
            "versions": h.stats.versions,
            "notes": h.stats.notes,
            "last_edited": h.stats.last_edited,
            "needs_why": h.stats.needs_why,
        },
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


def root_json(r: Root, marked: bool | None = None) -> dict[str, Any]:
    return {"id": r.id, "label": r.label, "location": r.location, "kind": r.kind,
            "search": r.search, "save": r.save, "marked": marked}  # fmt: skip


def _marked(eridi: Eridi, r: Root) -> bool:
    return eridi.root_marker(r.id) == r.id


def project_json(eridi: Eridi, p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "home_storage_id": p.home_root_id,
        "home_storage": eridi.root_label(p.home_root_id) if p.home_root_id else None,
        "folders": [
            {
                "ref": f"{f.root_id}:{f.path}",
                "storage_id": f.root_id,
                "storage": eridi.root_label(f.root_id),
                "path": f.path,
            }
            for f in p.folders
        ],
    }


# --- request bodies ---


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    file: str | None = None
    outcome: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    project: str | None = None


class OutcomeIn(BaseModel):
    outcome: str
    entry_id: str | None = None
    file: str | None = None
    text: str = Field(default="", max_length=20_000)


class DismissIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    home_storage_id: str | None = None
    folders: list[str] = Field(default_factory=list, max_length=50)


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    home_storage_id: str | None = None


class FolderIn(BaseModel):
    folder: str = Field(min_length=1, max_length=2000)
    """A folder path, a storage name (all of it), or `<storage ID>:<folder>`."""


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
        "last_activity": eridi.last_activity or scan.last_finished,
        "scan": {
            "running": scan.running,
            "last_started": scan.last_started,
            "last_finished": scan.last_finished,
            "last_error": scan.last_error,
            "last_summary": scan.last_summary,
        },
    }


@router.get("/search")
def search(request: Request, q: str = "", limit: int = 30, project: str | None = None) -> Any:
    try:
        hits = _eridi(request).search(q, max(1, min(limit, 100)), project or None)
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {"query": q, "results": [hit_json(h) for h in hits]}


@router.get("/changes")
def changes(request: Request, project: str | None = None) -> Any:
    try:
        pending = _eridi(request).pending_changes(project or None)
        return {"changes": [change_json(c) for c in pending]}
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
                               tags=body.tags, project=body.project or None)  # fmt: skip
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


def activity_json(a: Activity) -> dict[str, Any]:
    return {
        "kind": a.kind,
        "at": a.at,
        "file_id": f"{a.root_id}:{a.path}",
        "path": a.path,
        "title": a.title,
        "storage": a.root_label,
        "status": a.status,
        "summary": a.summary,
        "from": a.from_text_sha256,
        "to": a.to_text_sha256,
        "current": a.is_current,
        "needs_why": a.needs_why,
        "notes": [
            {"id": e.entry_id, "text": e.text, "author": e.author, "outcome": e.outcome}
            for e in a.explanations
        ],
    }


@router.get("/activity")
def activity(request: Request, limit: int = 50) -> Any:
    try:
        items = _eridi(request).activity(max(1, min(limit, 500)))
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {"activity": [activity_json(a) for a in items]}


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
def recent(request: Request, limit: int = 20, project: str | None = None) -> Any:
    eridi = _eridi(request)
    try:
        entries = eridi.recent(max(1, min(limit, 200)), project=project or None)
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
    eridi = _eridi(request)
    return {"storage": [root_json(r, _marked(eridi, r)) for r in eridi.list_roots()]}


@router.patch("/storage/{root_id}")
def update_storage(request: Request, root_id: str, body: StorageIn) -> Any:
    try:
        root = _eridi(request).update_root(
            root_id, label=body.label, search=body.search, save=body.save
        )
    except KNOWN_ERRORS as exc:
        return error(exc)
    return root_json(root)


@router.get("/projects")
def projects(request: Request) -> Any:
    eridi = _eridi(request)
    try:
        return {"projects": [project_json(eridi, p) for p in eridi.list_projects()]}
    except KNOWN_ERRORS as exc:
        return error(exc)


@router.post("/projects")
def create_project(request: Request, body: ProjectIn) -> Any:
    eridi = _eridi(request)
    try:
        p = eridi.create_project(body.name, home_root_id=body.home_storage_id, folders=body.folders)
    except KNOWN_ERRORS as exc:
        return error(exc)
    return JSONResponse(project_json(eridi, p), status_code=201)


@router.get("/projects/{project_id}")
def project(request: Request, project_id: str, limit: int = 100) -> Any:
    """A project page: files from all storage, changes needing a why, record timeline."""
    eridi = _eridi(request)
    try:
        o = eridi.project_overview(project_id, max(1, min(limit, 500)))
    except KNOWN_ERRORS as exc:
        return error(exc)
    return {
        **project_json(eridi, o.project),
        "files": [file_json(f, eridi.root_label(f.root_id)) for f in o.files[:500]],
        "file_count": len(o.files),
        "changes": [change_json(c) for c in o.pending],
        "entries": [entry_json(eridi, e, "current") for e in o.entries],
    }


@router.patch("/projects/{project_id}")
def update_project(request: Request, project_id: str, body: ProjectUpdateIn) -> Any:
    eridi = _eridi(request)
    try:
        p = eridi.get_project(project_id)
        if body.name is not None and body.name != p.name:
            p = eridi.rename_project(p.id, body.name)
        if "home_storage_id" in body.model_fields_set and body.home_storage_id != p.home_root_id:
            p = eridi.set_project_home(p.id, body.home_storage_id)
    except KNOWN_ERRORS as exc:
        return error(exc)
    return project_json(eridi, p)


@router.post("/projects/{project_id}/folders")
def add_project_folder(request: Request, project_id: str, body: FolderIn) -> Any:
    eridi = _eridi(request)
    try:
        return project_json(eridi, eridi.add_project_folder(project_id, body.folder))
    except KNOWN_ERRORS as exc:
        return error(exc)


@router.post("/projects/{project_id}/folders/remove")
def remove_project_folder(request: Request, project_id: str, body: FolderIn) -> Any:
    eridi = _eridi(request)
    try:
        return project_json(eridi, eridi.remove_project_folder(project_id, body.folder))
    except KNOWN_ERRORS as exc:
        return error(exc)


@router.post("/storage/{root_id}/mark")
def mark_storage(request: Request, root_id: str) -> Any:
    """Write the folder marker so other computers and labmates recognize this folder."""
    eridi = _eridi(request)
    try:
        root = eridi.mark_root(root_id)
    except KNOWN_ERRORS as exc:
        return error(exc)
    return root_json(root, True)


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
