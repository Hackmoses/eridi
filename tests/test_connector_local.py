"""M2.2: the local folder connector."""

import builtins
import io
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import SampleLab
from eridi.connectors.base import Connector, ConnectorError, PlaceholderNotReadError
from eridi.connectors.local import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    LocalConnector,
    is_placeholder,
)


def paths(connector: LocalConnector) -> list[str]:
    return [f.path for f in connector.list_files()]


@pytest.fixture
def lab(sample_lab: SampleLab) -> LocalConnector:
    return LocalConnector(sample_lab.root, "root_LAB")


def test_is_a_connector(lab: LocalConnector) -> None:
    assert isinstance(lab, Connector)
    assert lab.root_id() == "root_LAB"
    assert lab.kind == "local"


def test_lists_sample_lab(lab: LocalConnector, sample_lab: SampleLab) -> None:
    listed = paths(lab)
    assert listed == paths(lab)  # same order every time
    assert "Protocols/Western_blot_blocking_v7.docx" in listed
    assert "Protokolle_Ü/Färbung ñ v2.md" in listed
    assert "Protocols/Cell culture SOP.gdoc" in listed
    assert not any("~$" in p for p in listed)  # Office temp file skipped
    by_path = {f.path: f for f in lab.list_files()}
    protocol = by_path["Protocols/Western_blot_blocking_v7.docx"]
    assert protocol.size == sample_lab.protocol_docx.stat().st_size
    assert protocol.modified == sample_lab.protocol_docx.stat().st_mtime
    assert not protocol.is_placeholder


def test_default_ignores(tmp_path: Path) -> None:
    for rel in [
        ".git/config",
        "node_modules/x/index.js",
        "a/__pycache__/m.pyc",
        ".DS_Store",
        "sub/Thumbs.db",
        "sub/desktop.ini",
        ".~lock.report.docx#",
        "._resource_fork",
        ".eridi-root.json",
        "keep/report.docx",
    ]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    assert paths(LocalConnector(tmp_path, "r")) == ["keep/report.docx"]


def test_custom_ignore_rules(tmp_path: Path) -> None:
    for rel in ["raw/big.tif", "raw/keep.txt", "notes/a.md", "notes/draft.tmp", "b.md"]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    (tmp_path / ".eridiignore").write_text(
        "# comment\nraw/\n!raw/keep.txt\n*.tmp\n", encoding="utf-8"
    )
    assert paths(LocalConnector(tmp_path, "r")) == ["b.md", "notes/a.md"]
    assert paths(LocalConnector(tmp_path, "r", ignore=["notes/"])) == ["b.md"]


def test_unicode_paths_round_trip(lab: LocalConnector, sample_lab: SampleLab) -> None:
    data = lab.open_bytes("Protokolle_Ü/Färbung ñ v2.md")
    assert data == sample_lab.unicode_doc.read_bytes()


def test_open_bytes_rejects_escaping_paths(lab: LocalConnector) -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        lab.open_bytes("../outside.txt")
    with pytest.raises(ConnectorError, match="Could not read"):
        lab.open_bytes("Notes/does_not_exist.md")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin rights on Windows")
def test_symlink_out_of_root_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "inner").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (root / "inner" / "real.txt").write_text("inside", encoding="utf-8")
    (root / "link_out.txt").symlink_to(outside / "secret.txt")
    (root / "dir_out").symlink_to(outside, target_is_directory=True)
    (root / "link_in.txt").symlink_to(root / "inner" / "real.txt")
    connector = LocalConnector(root, "r")
    assert sorted(paths(connector)) == ["inner/real.txt", "link_in.txt"]
    with pytest.raises(ConnectorError, match="outside"):
        connector.open_bytes("link_out.txt")


def test_unreadable_file_is_logged_and_skipped(
    lab: LocalConnector, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_stat = os.stat

    def flaky_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path).endswith("lab_meeting_2026-10-14.md"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky_stat)
    with caplog.at_level(logging.WARNING):
        listed = paths(lab)
    assert "Notes/lab_meeting_2026-10-14.md" not in listed
    assert "Protocols/Western_blot_blocking_v7.docx" in listed  # the listing carried on
    assert "lab_meeting_2026-10-14.md" in caplog.text
    assert "Switch blocking buffer" not in caplog.text  # never the file's contents


def test_unreadable_folder_is_logged_and_skipped(
    lab: LocalConnector, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_scandir = os.scandir

    def flaky_scandir(path: Any = ".") -> Any:
        if str(path).endswith("Analysis"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)
    with caplog.at_level(logging.WARNING):
        listed = paths(lab)
    assert not any(p.startswith("Analysis/") for p in listed)
    assert "Notes/lab_meeting_2026-10-14.md" in listed
    assert "Analysis" in caplog.text


def test_missing_root_explains(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError, match="can't be found"):
        list(LocalConnector(tmp_path / "unplugged drive", "r").list_files())


def test_never_opens_for_writing(lab: LocalConnector, monkeypatch: pytest.MonkeyPatch) -> None:
    modes: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        modes.append(mode)
        return real_open(file, mode, *args, **kwargs)

    def spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        modes.append("w" if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) else "r")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    for f in lab.list_files():
        lab.open_bytes(f.path)
    lab.changes_since(None)
    assert modes  # the spy saw the reads
    assert all(not set(m) & set("wax+") for m in modes), modes


def test_versions_are_empty(lab: LocalConnector) -> None:
    assert lab.versions("Notes/lab_meeting_2026-10-14.md") == []
    with pytest.raises(ConnectorError):
        lab.open_version("Notes/lab_meeting_2026-10-14.md", "1")


def test_changes_since(lab: LocalConnector, sample_lab: SampleLab) -> None:
    changes, cursor = lab.changes_since(None)
    assert {c.kind for c in changes} == {"added"}
    assert len(changes) == len(paths(lab))
    later = float(cursor) + 10
    os.utime(sample_lab.notes_md, (later, later))
    changes, _ = lab.changes_since(cursor)
    assert [(c.path, c.kind) for c in changes] == [("Notes/lab_meeting_2026-10-14.md", "modified")]


def _stat(**fields: int) -> os.stat_result:
    return SimpleNamespace(**fields)  # type: ignore[return-value]


def test_placeholder_detection() -> None:
    assert is_placeholder(_stat(st_file_attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS), "win32")
    assert is_placeholder(_stat(st_file_attributes=FILE_ATTRIBUTE_OFFLINE), "win32")
    assert not is_placeholder(_stat(st_file_attributes=0x20), "win32")  # archive bit only
    assert is_placeholder(_stat(st_size=5000, st_blocks=0), "darwin")
    assert not is_placeholder(_stat(st_size=0, st_blocks=0), "darwin")  # really empty
    assert not is_placeholder(_stat(st_size=5000, st_blocks=16), "darwin")
    assert not is_placeholder(_stat(st_size=5000, st_blocks=0), "linux")


def test_placeholders_are_listed_but_not_read(
    lab: LocalConnector, sample_lab: SampleLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eridi.connectors.local as local

    monkeypatch.setattr(local, "is_placeholder", lambda st: True)
    listed = list(lab.list_files())
    assert listed
    assert all(f.is_placeholder for f in listed)
    with pytest.raises(PlaceholderNotReadError, match="online-only"):
        lab.open_bytes("Notes/lab_meeting_2026-10-14.md")
    allowed = LocalConnector(sample_lab.root, "root_LAB", read_online_only_files=True)
    assert allowed.open_bytes("Notes/lab_meeting_2026-10-14.md").startswith(b"# Lab meeting")
