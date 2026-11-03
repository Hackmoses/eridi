"""M14.5: release tags must match the version, and every file gets a checksum."""

import hashlib
from pathlib import Path

from eridi import __version__
from test_engine import load_script


def test_tag_must_match_every_version() -> None:
    check = load_script("check_version")
    assert check.problems(f"v{__version__}") == []
    wrong = check.problems("v99.0.0")
    assert len(wrong) == 4 and "pyproject.toml says" in wrong[0]
    assert "look like v1.2.3" in check.problems("1.0.0")[0]


def test_checksums(tmp_path: Path) -> None:
    (tmp_path / "Eridi_0.2.0_amd64.deb").write_bytes(b"deb")
    (tmp_path / "latest.json").write_text("{}", "utf-8")
    text = load_script("checksums").checksums(tmp_path)
    assert text == (
        f"{hashlib.sha256(b'deb').hexdigest()}  Eridi_0.2.0_amd64.deb\n"
        f"{hashlib.sha256(b'{}').hexdigest()}  latest.json\n"
    )
