"""M14.2: the desktop app's configuration agrees with the engine and stays locked down."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
CONF = json.loads((DESKTOP / "src-tauri" / "tauri.conf.json").read_text("utf-8"))


def engine_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return str(data["project"]["version"])


def test_versions_match_everywhere() -> None:
    """One version for the engine, the app, its updater manifest and the Rust crate."""
    version = engine_version()
    cargo = tomllib.loads((DESKTOP / "src-tauri" / "Cargo.toml").read_text("utf-8"))
    package = json.loads((DESKTOP / "package.json").read_text("utf-8"))
    assert CONF["version"] == cargo["package"]["version"] == package["version"] == version


def test_engine_is_the_sidecar() -> None:
    assert CONF["bundle"]["externalBin"] == ["binaries/eridi-engine"]
    main = (DESKTOP / "src-tauri" / "src" / "main.rs").read_text("utf-8")
    assert '.sidecar("eridi-engine")' in main and '["serve", "--sidecar"]' in main


def test_engine_pages_get_no_shell_access() -> None:
    """The window shows the engine's pages over http://127.0.0.1; those must not be able to
    call into the desktop shell. Capabilities list no remote URLs and only core defaults."""
    for path in (DESKTOP / "src-tauri" / "capabilities").glob("*.json"):
        capability = json.loads(path.read_text("utf-8"))
        assert "remote" not in capability, path.name
        assert capability["permissions"] == ["core:default"], path.name
    assert CONF["app"].get("withGlobalTauri") is not True


def test_window_only_navigates_to_this_computer() -> None:
    main = (DESKTOP / "src-tauri" / "src" / "main.rs").read_text("utf-8")
    assert 'const LOCAL_PREFIX: &str = "http://127.0.0.1:";' in main
    assert ".filter(|u| u.starts_with(LOCAL_PREFIX))" in main


def test_icons_exist() -> None:
    for icon in CONF["bundle"]["icon"]:
        assert (DESKTOP / "src-tauri" / icon).is_file(), icon
