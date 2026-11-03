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
