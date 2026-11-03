import os
from pathlib import Path

import pytest

from eridi.core.paths import (
    PathOutsideRootError,
    app_home,
    from_posix_relative,
    normalize_relative,
    to_posix_relative,
)


def test_app_home_respects_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "eridi-home"
    monkeypatch.setenv("ERIDI_HOME", str(target))
    assert app_home() == target
    assert target.is_dir()


def test_relative_path_is_posix(tmp_path: Path) -> None:
    file = tmp_path / "Protocols" / "WB" / "v7.docx"
    file.parent.mkdir(parents=True)
    file.write_text("x")
    assert to_posix_relative(tmp_path, file) == "Protocols/WB/v7.docx"


def test_windows_style_input_normalizes() -> None:
    assert normalize_relative("Protocols\\WB\\v7.docx") == "Protocols/WB/v7.docx"
    assert normalize_relative("./Protocols//WB/v7.docx") == "Protocols/WB/v7.docx"


@pytest.mark.parametrize(
    "name", ["Protokolle_Ü/Blot ñ v2.docx", "实验/方案 1.md", "Lab notes (final).txt"]
)
def test_unicode_and_spaces_round_trip(tmp_path: Path, name: str) -> None:
    file = tmp_path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("x", encoding="utf-8")
    stored = to_posix_relative(tmp_path, file)
    assert stored == name
    assert from_posix_relative(tmp_path, stored).resolve() == file.resolve()


def test_path_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(PathOutsideRootError, match="outside the storage folder"):
        to_posix_relative(root, outside)
    with pytest.raises(PathOutsideRootError):
        to_posix_relative(root, root / ".." / "elsewhere.txt")


def test_root_itself_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideRootError, match="storage folder itself"):
        to_posix_relative(tmp_path, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_symlink_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    link = root / "link.txt"
    link.symlink_to(secret)
    with pytest.raises(PathOutsideRootError):
        to_posix_relative(root, link)


@pytest.mark.parametrize(
    "bad",
    ["", " a.txt", "/etc/passwd", "C:\\x.txt", "\\\\server\\share\\x", "../x", "a/../../x", "."],
)
def test_normalize_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative(bad)
