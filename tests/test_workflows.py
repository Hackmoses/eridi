"""M14: the release workflow builds, smoke-tests and (when configured) signs on every OS."""

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")
WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    return data


def steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s.get("name", s.get("uses", "")): s for s in job["steps"]}


def test_release_builds_every_target_and_smoke_tests_the_engine() -> None:
    release = load("release.yml")
    targets = {m["target"] for m in release["jobs"]["engine"]["strategy"]["matrix"]["include"]}
    assert targets == {"aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc",
                       "x86_64-unknown-linux-gnu"}  # fmt: skip
    engine = steps(release["jobs"]["engine"])
    assert "scripts/smoke_engine.py" in engine["Smoke test the packaged engine"]["run"]
    assert release["jobs"]["desktop"]["needs"] == "engine"


def test_signing_runs_only_with_secrets() -> None:
    release = load("release.yml")
    engine = steps(release["jobs"]["engine"])
    desktop = steps(release["jobs"]["desktop"])
    assert "HAS_APPLE == 'true'" in engine["macOS signing certificate"]["if"]
    assert "HAS_AZURE == 'true'" in engine["Sign the engine (Windows, Azure Trusted Signing)"]["if"]
    assert (
        "HAS_APPLE == 'true'" in desktop["macOS signing and notarization (Tauri reads these)"]["if"]
    )
    assert "--config release.conf.json" in desktop["Build installers"]["run"]


def test_ci_checks_the_desktop_shell() -> None:
    ci = steps(load("ci.yml")["jobs"]["desktop"])
    assert ci["Lint"]["run"] == "cargo clippy --all-targets -- -D warnings"
