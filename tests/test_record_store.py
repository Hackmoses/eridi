"""M1.2: RecordStore writes atomically and reads defensively."""

import json
import logging
import os
from pathlib import Path

import pytest

from conftest import entry
from eridi.core.record.schema import serialize_entry
from eridi.core.record.store import (
    MARKER_NAME,
    NEWER_VERSION_WARNING,
    RecordFolderError,
    RecordStore,
)


@pytest.fixture
def store(tmp_path: Path) -> RecordStore:
    return RecordStore.init(tmp_path / "record")


def test_init_writes_marker(tmp_path: Path) -> None:
    folder = tmp_path / "Eridi"
    store = RecordStore.init(folder)
    marker = json.loads((folder / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["schema"] == "eridi.recordset/1"
    assert marker["id"] == store.id
    assert marker["created_at"].endswith("Z")
    assert (folder / "entries").is_dir()


def test_init_twice_keeps_the_same_marker(tmp_path: Path) -> None:
    first = RecordStore.init(tmp_path / "r")
    second = RecordStore.init(tmp_path / "r")
    assert first.id == second.id


def test_opening_a_plain_folder_explains(tmp_path: Path) -> None:
    with pytest.raises(RecordFolderError, match="not a record folder"):
        RecordStore(tmp_path)


def test_append_then_read(store: RecordStore) -> None:
    written = [entry(text=f"note {i}") for i in range(5)]
    for e in reversed(written):
        store.append(e)
    read = store.read()
    assert read.entries == written  # sorted by ID
    assert read.problems == []
    assert list(store.iter_entries()) == written
    assert store.get(written[2].id) == written[2]
    assert store.get("01JC8Z9K3V5W6X7Y8Z9A0B1C2D") is None
    assert store.get("nope") is None


def test_layout_and_format(store: RecordStore) -> None:
    e = entry()
    path = store.append(e)
    created = e.created
    assert path == store.folder / "entries" / f"{created.year:04d}" / f"{created.month:02d}" / (
        f"{e.id}.json"
    )
    assert path.read_bytes() == serialize_entry(e).encode("utf-8")
    assert not any((store.folder / "entries" / ".tmp").iterdir())


def test_read_after(store: RecordStore) -> None:
    entries = [entry(text=str(i)) for i in range(4)]
    for e in entries:
        store.append(e)
    assert store.read(after=entries[1].id).entries == entries[2:]


def test_never_overwrites(store: RecordStore) -> None:
    e = entry()
    path = store.append(e)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        store.append(e)
    assert path.read_bytes() == before


def test_crash_between_temp_write_and_rename(
    store: RecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(src: object, dst: object) -> None:
        raise KeyboardInterrupt("power cut")

    for name in ("replace", "rename", "link"):
        monkeypatch.setattr(os, name, crash)
    with pytest.raises(KeyboardInterrupt):
        store.append(entry())
    monkeypatch.undo()
    read = store.read()
    assert read.entries == []
    assert read.problems == []


def test_leftover_temp_files_are_ignored(store: RecordStore) -> None:
    tmp = store.folder / "entries" / ".tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "01JC8Z9K3V5W6X7Y8Z9A0B1C2D.json.123.tmp").write_text("{half", encoding="utf-8")
    assert store.read().entries == []
    assert store.read().problems == []


@pytest.mark.parametrize(
    "suffix", [" (1).json", "-DESKTOP-X.json", " - Copy.json", ".json.conflict"]
)
def test_conflict_copies_are_ignored_and_reported(store: RecordStore, suffix: str) -> None:
    e = entry()
    path = store.append(e)
    copy = path.with_name(e.id + suffix)
    copy.write_bytes(path.read_bytes())
    read = store.read()
    assert read.entries == [e]
    assert [p.path for p in read.problems] == [copy]
    assert "conflict" in read.problems[0].reason


def test_os_junk_and_hidden_files_are_not_reported(store: RecordStore) -> None:
    month = store.append(entry()).parent
    (month / ".DS_Store").write_bytes(b"x")
    (month / "desktop.ini").write_bytes(b"x")
    assert store.read().problems == []


def test_invalid_json_is_skipped_and_reported(store: RecordStore) -> None:
    good = entry()
    month = store.append(good).parent
    bad = month / "01JC8Z9K3V5W6X7Y8Z9A0B1C2D.json"
    bad.write_text('{"schema": "eridi.record/1", ', encoding="utf-8")
    read = store.read()
    assert read.entries == [good]
    assert len(read.problems) == 1
    assert read.problems[0].path == bad
    assert "invalid JSON" in read.problems[0].reason


def test_missing_field_is_skipped_and_reported(store: RecordStore) -> None:
    e = entry()
    path = store.append(e)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["author"]
    other = path.with_name("01JC8Z9K3V5W6X7Y8Z9A0B1C2D.json")
    data["id"] = other.stem
    other.write_text(json.dumps(data), encoding="utf-8")
    read = store.read()
    assert read.entries == [e]
    assert "author" in read.problems[0].reason


def test_id_must_match_file_name(store: RecordStore) -> None:
    e = entry()
    path = store.append(e)
    renamed = path.with_name("01JC8Z9K3V5W6X7Y8Z9A0B1C2D.json")
    path.rename(renamed)
    read = store.read()
    assert read.entries == []
    assert "does not match" in read.problems[0].reason


def test_newer_schema_skipped_with_a_single_warning(
    store: RecordStore, caplog: pytest.LogCaptureFixture
) -> None:
    good = entry()
    store.append(good)
    month = store.path_for(good).parent
    for e in (entry(), entry()):
        data = json.loads(serialize_entry(e))
        data["schema"] = "eridi.record/2"
        (month / f"{e.id}.json").write_text(json.dumps(data), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        read = store.read()
    assert read.entries == [good]
    assert len(read.newer_version) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [NEWER_VERSION_WARNING]


def test_logs_never_contain_entry_text(
    store: RecordStore, caplog: pytest.LogCaptureFixture
) -> None:
    private = "Confidential patient cohort detail"
    path = store.append(entry(text=private))
    path.with_name(path.stem + " (1).json").write_text(path.read_text("utf-8"), encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        store.read()
    assert private not in caplog.text


def test_no_overwrite_without_hard_links(
    store: RecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(src: object, dst: object) -> None:
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", no_links)
    e = entry()
    path = store.append(e)
    with pytest.raises(FileExistsError):
        store.append(e)
    assert store.read().entries == [e]
    assert path.is_file()
