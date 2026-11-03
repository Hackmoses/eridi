"""M14.1: the engine as the desktop app runs it: health endpoint and the sidecar protocol.

The same smoke test runs against each packaged binary in the release workflow
(`scripts/smoke_engine.py <binary>`); here it runs against the source tree.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from conftest import SampleLab
from eridi import __version__
from eridi.api.app import create_app
from eridi.api.scanner import BackgroundScanner
from eridi.core.service import Eridi
from test_api import BASE, PORT, TOKEN

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_health_needs_no_token_and_says_only_the_version() -> None:
    with Eridi() as eridi:
        app = create_app(eridi, token=TOKEN, port=PORT, scanner=BackgroundScanner(eridi, 0))
        with TestClient(app, base_url=BASE) as client:
            r = client.get("/api/health")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "app": "eridi", "version": __version__}
            assert client.get("/api/status").status_code == 401  # everything else still needs it
            assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
            assert client.post("/api/health").status_code == 401


def test_sidecar_smoke_from_source() -> None:
    """Start, health, parallel scan with PDF text, a new link, and stop when stdin closes."""
    health = load_script("smoke_engine").smoke(None)
    assert health["version"] == __version__


def test_build_script_names_the_binary_for_tauri(monkeypatch: pytest.MonkeyPatch) -> None:
    build = load_script("build_engine")
    monkeypatch.setenv("ERIDI_TARGET_TRIPLE", "aarch64-apple-darwin")
    assert build.target_triple() == "aarch64-apple-darwin"
    assert build.OUT == ROOT / "desktop" / "src-tauri" / "binaries"


def test_first_run_without_a_terminal(sample_lab: SampleLab, tmp_path: Path) -> None:
    """The desktop app's user never runs `eridi init` or `eridi storage add`."""
    with Eridi() as eridi:
        assert not eridi.initialized
        app = create_app(eridi, token=TOKEN, port=PORT, scanner=BackgroundScanner(eridi, 0))
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(app, base_url=BASE) as client:
            bad = client.post("/api/setup", json={"name": "  "}, headers=auth)
            assert bad.status_code in (400, 422)
            body = {"name": "B. Lee", "record_folder": str(tmp_path / "rec")}
            r = client.post("/api/setup", json=body, headers=auth)
            assert r.status_code == 200 and r.json()["user"] == "B. Lee"
            assert client.get("/api/status", headers=auth).json()["initialized"]
            added = client.post("/api/storage", json={"path": str(sample_lab.root)}, headers=auth)
            assert added.status_code == 201 and added.json()["label"] == sample_lab.root.name
            again = client.post("/api/storage", json={"path": str(sample_lab.root)}, headers=auth)
            assert again.status_code == 400 and "already connected" in again.json()["error"]
            missing = client.post("/api/storage", json={"path": str(tmp_path / "nope")},
                                  headers=auth)  # fmt: skip
            assert missing.status_code == 400 and "not a folder" in missing.json()["error"]
