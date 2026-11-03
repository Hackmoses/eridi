"""M1.1: the record schema, per docs/RECORD_FORMAT.md."""

import json
from pathlib import Path
from typing import Any

import pytest

from eridi.core.ids import is_ulid
from eridi.core.models import Author, Subject
from eridi.core.record.schema import (
    RecordFormatError,
    UnsupportedSchemaError,
    entry_to_dict,
    make_entry,
    parse_entry,
    serialize_entry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "record" / "v1"
KIND_FIXTURES = ["note", "decision", "outcome", "link", "project", "tombstone"]


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def minimal() -> dict[str, Any]:
    return {
        "schema": "eridi.record/1",
        "id": "01JC8Z9K3V5W6X7Y8Z9A0B1C2D",
        "kind": "note",
        "created_at": "2026-11-03T14:05:22.481Z",
        "author": {"name": "A. Okafor", "kind": "human"},
        "device_id": "dev_01JC8Y0000000000000000000A",
        "subjects": [],
        "text": "Why this changed.",
    }


@pytest.mark.parametrize("name", KIND_FIXTURES)
def test_fixture_per_kind_parses(name: str) -> None:
    entry = parse_entry(load(name))
    assert entry.kind == name
    assert entry.is_supported_kind


@pytest.mark.parametrize(
    "name", [*KIND_FIXTURES, "unknown_fields", "project_folders", "project_labels"]
)
def test_round_trip(name: str) -> None:
    entry = parse_entry(load(name))
    again = parse_entry(json.loads(serialize_entry(entry)))
    assert again == entry
    assert serialize_entry(again) == serialize_entry(entry)


def test_note_fields() -> None:
    entry = parse_entry(load("note"))
    assert entry.author == Author("A. Okafor", "human", "a.okafor@example.edu")
    assert entry.subjects[0].path == "Protocols/Western_blot_blocking_v7.docx"
    assert entry.change is not None
    assert entry.change.summary == "Primary antibody incubation: 1 h at RT → 2 h at RT"
    assert entry.outcome == "worked"
    assert entry.tags == ("antibody", "lot-change")
    assert entry.created.year == 2026


def test_serialized_layout() -> None:
    text = serialize_entry(parse_entry(minimal()))
    assert text.endswith("}\n")
    assert '\n  "author": {\n    "kind": "human",' in text
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_unicode_is_written_as_utf8_not_escaped() -> None:
    data = minimal() | {"text": "Färbung → 2 h"}
    assert "Färbung → 2 h" in serialize_entry(parse_entry(data))


def test_unknown_fields_preserved() -> None:
    entry = parse_entry(load("unknown_fields"))
    assert not entry.is_supported_kind
    assert entry.extra_unknown == {"future_field": {"nested": [1, 2, 3]}}
    assert entry.author.extra_unknown == {"orcid": "0000-0002-1825-0097"}
    assert entry.subjects[0].extra_unknown == {"size_hint": 12}
    out = entry_to_dict(entry)
    assert out["future_field"] == {"nested": [1, 2, 3]}
    assert out["author"]["orcid"] == "0000-0002-1825-0097"
    assert out["subjects"][0]["size_hint"] == 12


REQUIRED = ["schema", "id", "kind", "created_at", "author", "device_id", "subjects", "text"]


@pytest.mark.parametrize("key", REQUIRED)
def test_missing_required_field_names_it(key: str) -> None:
    data = minimal()
    del data[key]
    with pytest.raises(RecordFormatError) as info:
        parse_entry(data)
    assert info.value.field == key
    assert key in str(info.value)


@pytest.mark.parametrize("key", ["name", "kind"])
def test_missing_author_field_names_it(key: str) -> None:
    data = minimal()
    del data["author"][key]
    with pytest.raises(RecordFormatError, match=f"author.{key}"):
        parse_entry(data)


@pytest.mark.parametrize("key", ["root_id", "path"])
def test_missing_subject_field_names_it(key: str) -> None:
    data = minimal()
    data["subjects"] = [{"root_id": "root_x", "path": "a.md"}]
    del data["subjects"][0][key]
    with pytest.raises(RecordFormatError, match=rf"subjects\[0\]\.{key}"):
        parse_entry(data)


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("tombstone", "target_id"),
        ("outcome", "target_id"),
        ("link", "links"),
        ("project", "project"),
    ],
)
def test_kind_specific_fields_required(kind: str, field: str) -> None:
    data = minimal() | {"kind": kind, "text": ""}
    with pytest.raises(RecordFormatError) as info:
        parse_entry(data)
    assert info.value.field == field


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"id": "not-a-ulid"}, "id"),
        ({"id": "01jc8z9k3v5w6x7y8z9a0b1c2d"}, "id"),
        ({"created_at": "2026-11-03 14:05"}, "created_at"),
        ({"created_at": "2026-11-03T14:05:22+02:00"}, "created_at"),
        ({"author": {"name": "X", "kind": "robot"}}, "author.kind"),
        ({"subjects": "Protocols/a.docx"}, "subjects"),
        ({"subjects": [{"root_id": "r", "path": "Protocols\\a.docx"}]}, "subjects[0].path"),
        ({"subjects": [{"root_id": "r", "path": "../a.docx"}]}, "subjects[0].path"),
        ({"text": "   "}, "text"),
        ({"text": 3}, "text"),
        ({"outcome": "great"}, "outcome"),
        ({"tags": "antibody"}, "tags"),
        ({"supersedes": "abc"}, "supersedes"),
        ({"change": {"from_text_sha256": "a"}}, "change.to_text_sha256"),
        ({"schema": "something/1"}, "schema"),
        ({"extra": []}, "extra"),
    ],
)
def test_invalid_values_name_the_field(patch: dict[str, Any], field: str) -> None:
    with pytest.raises(RecordFormatError) as info:
        parse_entry(minimal() | patch)
    assert info.value.field == field


def test_newer_major_schema_is_unsupported() -> None:
    with pytest.raises(UnsupportedSchemaError, match="newer version"):
        parse_entry(minimal() | {"schema": "eridi.record/2"})


def test_not_an_object() -> None:
    with pytest.raises(RecordFormatError, match="entry"):
        parse_entry(["not", "an", "object"])


def test_make_entry() -> None:
    entry = make_entry(
        "note",
        Author("A. Okafor"),
        "dev_01JC8Y0000000000000000000A",
        "Longer incubation.",
        subjects=[Subject("root_x", "Protocols/a.docx")],
        tags=["Antibody", "antibody", " Lot "],
    )
    assert is_ulid(entry.id)
    assert entry.created_at.endswith("Z")
    assert entry.tags == ("antibody", "lot")
    assert parse_entry(json.loads(serialize_entry(entry))) == entry


def test_make_entry_rejects_invalid() -> None:
    with pytest.raises(RecordFormatError, match="text"):
        make_entry("note", Author("A"), "dev_x", "")
    with pytest.raises(TypeError, match="colour"):
        make_entry("note", Author("A"), "dev_x", "t", colour="red")


def test_project_folders_fixture() -> None:
    entry = parse_entry(load("project_folders"))
    assert entry.project is not None and entry.project.folders is not None
    assert [(f.root_id[-1], f.path) for f in entry.project.folders] == [
        ("A", "Protocols/WB"),
        ("B", ""),
    ]
    # The older project fixture has no folder list: "not stated", not "none".
    older = parse_entry(load("project"))
    assert older.project is not None and older.project.folders is None


def test_project_labels_fixture() -> None:
    entry = parse_entry(load("project_labels"))
    assert entry.project is not None and entry.project.labels == ("patient-data",)
    assert entry.project.folders is None  # labels and folders are independent
    for older in ("project", "project_folders"):  # entries before M11: labels not stated
        project = parse_entry(load(older)).project
        assert project is not None and project.labels is None


@pytest.mark.parametrize(
    ("folder", "field"),
    [
        ({"path": "x"}, "project.folders[0].root_id"),
        ({"root_id": "root_x", "path": "../up"}, "project.folders[0].path"),
        ({"root_id": "root_x", "path": "a\\b"}, "project.folders[0].path"),
    ],
)
def test_invalid_project_folders_name_the_field(folder: dict[str, Any], field: str) -> None:
    data = load("project_folders")
    data["project"]["folders"] = [folder]
    with pytest.raises(RecordFormatError) as info:
        parse_entry(data)
    assert info.value.field == field
