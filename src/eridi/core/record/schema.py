"""Parse, validate and serialize record entries (schema `eridi.record/1`).

The contract is `docs/RECORD_FORMAT.md`. Parsing is strict about required fields and their
types, and lenient about everything else: unknown fields and unknown kinds are kept, so
nothing a newer version wrote is ever lost.
"""

import json
from dataclasses import fields, replace
from typing import Any, NoReturn

from eridi.core.ids import is_ulid, new_ulid
from eridi.core.models import (
    AUTHOR_KINDS,
    JSON,
    OUTCOMES,
    Author,
    Entry,
    EntryChange,
    Link,
    ProjectFolder,
    ProjectRef,
    Subject,
    parse_timestamp,
    utc_now,
)
from eridi.core.paths import normalize_relative

SCHEMA_FAMILY = "eridi.record"
SUPPORTED_MAJOR = 1

_ENTRY_KEYS = {
    "schema",
    "id",
    "kind",
    "created_at",
    "author",
    "device_id",
    "project_id",
    "subjects",
    "change",
    "text",
    "outcome",
    "tags",
    "supersedes",
    "target_id",
    "links",
    "project",
    "extra",
}
_KINDS_NEEDING_TEXT = ("note", "decision")


class RecordFormatError(ValueError):
    """An entry does not follow the record format. `field` names the offending field."""

    def __init__(self, field: str, problem: str) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"{field}: {problem}")


class UnsupportedSchemaError(ValueError):
    """An entry was written with a newer major schema version than this reader knows."""

    def __init__(self, schema: str) -> None:
        self.schema = schema
        super().__init__(
            f"Entry uses schema {schema!r}, written by a newer version of Eridi. "
            "Update Eridi to read it."
        )


def _fail(field: str, problem: str) -> NoReturn:
    raise RecordFormatError(field, problem)


def _require(data: JSON, key: str, where: str) -> Any:
    if key not in data:
        _fail(f"{where}{key}", "required field is missing")
    return data[key]


def _string(value: Any, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        _fail(field, f"must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        _fail(field, "must not be empty")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _object(value: Any, field: str) -> JSON:
    if not isinstance(value, dict):
        _fail(field, f"must be an object, got {type(value).__name__}")
    return value


def _unknown(data: JSON, known: set[str]) -> JSON:
    return {k: v for k, v in data.items() if k not in known}


def check_schema(value: Any) -> None:
    """Raise unless `value` is a schema string this reader understands."""
    schema = _string(value, "schema")
    family, sep, version = schema.partition("/")
    if family != SCHEMA_FAMILY or not sep or not version.isdigit():
        _fail("schema", f"{schema!r} is not an Eridi record schema")
    if int(version) > SUPPORTED_MAJOR:
        raise UnsupportedSchemaError(schema)
    if int(version) < 1:
        _fail("schema", f"{schema!r} is not a known version")


def _parse_author(value: Any) -> Author:
    data = _object(value, "author")
    name = _string(_require(data, "name", "author."), "author.name", allow_empty=False)
    kind = _string(_require(data, "kind", "author."), "author.kind")
    if kind not in AUTHOR_KINDS:
        _fail("author.kind", f"must be one of {', '.join(AUTHOR_KINDS)}, got {kind!r}")
    email = _optional_string(data.get("email"), "author.email")
    return Author(name, kind, email, _unknown(data, {"name", "kind", "email"}))


def _parse_subject(value: Any, where: str) -> Subject:
    data = _object(value, where)
    root_id = _string(_require(data, "root_id", f"{where}."), f"{where}.root_id", allow_empty=False)
    raw_path = _string(_require(data, "path", f"{where}."), f"{where}.path")
    try:
        path = normalize_relative(raw_path)
    except ValueError as exc:
        _fail(f"{where}.path", str(exc))
    if path != raw_path:
        _fail(f"{where}.path", f"must be POSIX-style and normalized, e.g. {path!r}")
    known = {"root_id", "path", "root_label", "content_sha256", "text_sha256"}
    return Subject(
        root_id=root_id,
        path=path,
        root_label=_optional_string(data.get("root_label"), f"{where}.root_label"),
        content_sha256=_optional_string(data.get("content_sha256"), f"{where}.content_sha256"),
        text_sha256=_optional_string(data.get("text_sha256"), f"{where}.text_sha256"),
        extra_unknown=_unknown(data, known),
    )


def _parse_change(value: Any) -> EntryChange:
    data = _object(value, "change")
    to_sha = _string(
        _require(data, "to_text_sha256", "change."), "change.to_text_sha256", allow_empty=False
    )
    known = {"from_text_sha256", "to_text_sha256", "summary"}
    return EntryChange(
        from_text_sha256=_optional_string(data.get("from_text_sha256"), "change.from_text_sha256"),
        to_text_sha256=to_sha,
        summary=_optional_string(data.get("summary"), "change.summary"),
        extra_unknown=_unknown(data, known),
    )


def _parse_link_end(value: Any, field: str) -> str | JSON:
    if isinstance(value, str):
        return _string(value, field, allow_empty=False)
    if isinstance(value, dict):
        _parse_subject(value, field)  # validate; keep the original object
        return dict(value)
    _fail(field, "must be an entry ID or a subject object")


def _parse_link(value: Any, where: str) -> Link:
    data = _object(value, where)
    relation = _string(_require(data, "relation", f"{where}."), f"{where}.relation")
    return Link(
        from_=_parse_link_end(_require(data, "from", f"{where}."), f"{where}.from"),
        to=_parse_link_end(_require(data, "to", f"{where}."), f"{where}.to"),
        relation=relation,
        extra_unknown=_unknown(data, {"from", "to", "relation"}),
    )


def _parse_project_folder(value: Any, where: str) -> ProjectFolder:
    data = _object(value, where)
    root_id = _string(_require(data, "root_id", f"{where}."), f"{where}.root_id", allow_empty=False)
    raw_path = _string(data.get("path", ""), f"{where}.path")
    path = ""
    if raw_path:
        try:
            path = normalize_relative(raw_path)
        except ValueError as exc:
            _fail(f"{where}.path", str(exc))
        if path != raw_path:
            _fail(f"{where}.path", f"must be POSIX-style and normalized, e.g. {path!r}")
    return ProjectFolder(root_id, path, _unknown(data, {"root_id", "path"}))


def _parse_project(value: Any) -> ProjectRef:
    data = _object(value, "project")
    folders = None
    if data.get("folders") is not None:
        folders = tuple(
            _parse_project_folder(item, f"project.folders[{i}]")
            for i, item in enumerate(_parse_list(data["folders"], "project.folders"))
        )
    return ProjectRef(
        id=_string(_require(data, "id", "project."), "project.id", allow_empty=False),
        name=_string(_require(data, "name", "project."), "project.name", allow_empty=False),
        home_root_id=_optional_string(data.get("home_root_id"), "project.home_root_id"),
        folders=folders,
        extra_unknown=_unknown(data, {"id", "name", "home_root_id", "folders"}),
    )


def _parse_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(field, f"must be a list, got {type(value).__name__}")
    return value


def parse_entry(data: Any) -> Entry:
    """Validate a decoded JSON object and return an `Entry`.

    Raises `UnsupportedSchemaError` for newer major versions and `RecordFormatError`
    (naming the field) for anything else wrong.
    """
    data = _object(data, "entry")
    check_schema(_require(data, "schema", ""))
    entry_id = _string(_require(data, "id", ""), "id")
    if not is_ulid(entry_id):
        _fail("id", f"{entry_id!r} is not an uppercase 26-character ULID")
    kind = _string(_require(data, "kind", ""), "kind", allow_empty=False)
    created_at = _string(_require(data, "created_at", ""), "created_at")
    try:
        parse_timestamp(created_at)
    except ValueError:
        _fail("created_at", f"{created_at!r} is not a UTC ISO 8601 timestamp ending in 'Z'")
    author = _parse_author(_require(data, "author", ""))
    device_id = _string(_require(data, "device_id", ""), "device_id", allow_empty=False)
    subjects = tuple(
        _parse_subject(item, f"subjects[{i}]")
        for i, item in enumerate(_parse_list(_require(data, "subjects", ""), "subjects"))
    )
    text = _string(_require(data, "text", ""), "text")
    if kind in _KINDS_NEEDING_TEXT and not text.strip():
        _fail("text", f"must not be empty for a {kind} entry")

    outcome = _optional_string(data.get("outcome"), "outcome")
    if outcome is not None and outcome not in OUTCOMES:
        _fail("outcome", f"must be one of {', '.join(OUTCOMES)} or null, got {outcome!r}")
    tags_raw = data.get("tags")
    tags = (
        ()
        if tags_raw is None
        else tuple(
            _string(t, f"tags[{i}]", allow_empty=False)
            for i, t in enumerate(_parse_list(tags_raw, "tags"))
        )
    )
    supersedes = _optional_string(data.get("supersedes"), "supersedes")
    if supersedes is not None and not is_ulid(supersedes):
        _fail("supersedes", f"{supersedes!r} is not an entry ID")
    target_id = _optional_string(data.get("target_id"), "target_id")
    if target_id is not None and not is_ulid(target_id):
        _fail("target_id", f"{target_id!r} is not an entry ID")
    change = None if data.get("change") is None else _parse_change(data["change"])
    links = (
        None
        if data.get("links") is None
        else tuple(
            _parse_link(item, f"links[{i}]")
            for i, item in enumerate(_parse_list(data["links"], "links"))
        )
    )
    project = None if data.get("project") is None else _parse_project(data["project"])
    extra = {} if data.get("extra") is None else dict(_object(data["extra"], "extra"))

    if kind == "tombstone" and target_id is None:
        _fail("target_id", "required for a tombstone entry")
    if kind == "outcome" and target_id is None and not subjects:
        _fail("target_id", "an outcome entry needs a target_id or at least one subject")
    if kind == "link" and not links:
        _fail("links", "required for a link entry")
    if kind == "project" and project is None:
        _fail("project", "required for a project entry")

    return Entry(
        schema=data["schema"],
        id=entry_id,
        kind=kind,
        created_at=created_at,
        author=author,
        device_id=device_id,
        project_id=_optional_string(data.get("project_id"), "project_id"),
        subjects=subjects,
        change=change,
        text=text,
        outcome=outcome,
        tags=tags,
        supersedes=supersedes,
        target_id=target_id,
        links=links,
        project=project,
        extra=extra,
        extra_unknown=_unknown(data, _ENTRY_KEYS),
    )


def _with_unknown(known: JSON, unknown: JSON) -> JSON:
    # Known fields win, so an unknown key can never shadow one we understand.
    return {**unknown, **known}


def _subject_dict(s: Subject) -> JSON:
    out: JSON = {"root_id": s.root_id, "path": s.path}
    for key in ("root_label", "content_sha256", "text_sha256"):
        value = getattr(s, key)
        if value is not None:
            out[key] = value
    return _with_unknown(out, s.extra_unknown)


def entry_to_dict(entry: Entry) -> JSON:
    """The JSON object for an entry. Kind-specific fields appear only when set."""
    author: JSON = {"name": entry.author.name, "kind": entry.author.kind}
    if entry.author.email is not None:
        author["email"] = entry.author.email
    out: JSON = {
        "schema": entry.schema,
        "id": entry.id,
        "kind": entry.kind,
        "created_at": entry.created_at,
        "author": _with_unknown(author, entry.author.extra_unknown),
        "device_id": entry.device_id,
        "project_id": entry.project_id,
        "subjects": [_subject_dict(s) for s in entry.subjects],
        "change": None,
        "text": entry.text,
        "outcome": entry.outcome,
        "tags": list(entry.tags),
        "supersedes": entry.supersedes,
        "extra": dict(entry.extra),
    }
    if entry.change is not None:
        c = entry.change
        out["change"] = _with_unknown(
            {
                "from_text_sha256": c.from_text_sha256,
                "to_text_sha256": c.to_text_sha256,
                "summary": c.summary,
            },
            c.extra_unknown,
        )
    if entry.target_id is not None:
        out["target_id"] = entry.target_id
    if entry.links is not None:
        out["links"] = [
            _with_unknown(
                {"from": ln.from_, "to": ln.to, "relation": ln.relation}, ln.extra_unknown
            )
            for ln in entry.links
        ]
    if entry.project is not None:
        p = entry.project
        project: JSON = {"id": p.id, "name": p.name, "home_root_id": p.home_root_id}
        if p.folders is not None:
            project["folders"] = [
                _with_unknown({"root_id": f.root_id, "path": f.path}, f.extra_unknown)
                for f in p.folders
            ]
        out["project"] = _with_unknown(project, p.extra_unknown)
    return _with_unknown(out, entry.extra_unknown)


def serialize_entry(entry: Entry) -> str:
    """UTF-8 JSON text: keys sorted, 2-space indent, trailing newline."""
    return json.dumps(entry_to_dict(entry), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def make_entry(kind: str, author: Author, device_id: str, text: str = "", **fields_: Any) -> Entry:
    """Create a new, validated entry with a fresh ID and timestamp.

    Tags are lowercased. The result is round-tripped through `parse_entry`, so an invalid
    entry can never be created.
    """
    allowed = {f.name for f in fields(Entry)} - {"id", "kind", "created_at", "schema"}
    unknown = set(fields_) - allowed
    if unknown:
        raise TypeError(f"Unknown entry fields: {', '.join(sorted(unknown))}")
    if "tags" in fields_:
        fields_["tags"] = tuple(sorted({t.strip().lower() for t in fields_["tags"]}))
    if "subjects" in fields_:
        fields_["subjects"] = tuple(fields_["subjects"])
    entry = Entry(
        id=new_ulid(),
        kind=kind,
        created_at=utc_now(),
        author=author,
        device_id=device_id,
        text=text,
    )
    entry = replace(entry, **fields_)
    return parse_entry(json.loads(serialize_entry(entry)))
