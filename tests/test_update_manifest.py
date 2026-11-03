"""M14.4: update bundles are collected under unique names and listed in latest.json."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from test_engine import load_script


def fake_bundle(root: Path, files: list[str]) -> Path:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"signature of {path.name}" if name.endswith(".sig") else "bytes", "utf-8")
    return root


def test_collect_and_manifest(tmp_path: Path) -> None:
    collect = load_script("collect_bundles").collect
    out = tmp_path / "out"
    mac = ["dmg/Eridi_0.2.0_aarch64.dmg", "macos/Eridi.app.tar.gz", "macos/Eridi.app.tar.gz.sig"]
    collect("aarch64-apple-darwin", fake_bundle(tmp_path / "arm", mac), out, "0.2.0")
    intel = ["dmg/Eridi_0.2.0_x64.dmg", "macos/Eridi.app.tar.gz", "macos/Eridi.app.tar.gz.sig"]
    collect("x86_64-apple-darwin", fake_bundle(tmp_path / "x64", intel), out, "0.2.0")
    win = ["nsis/Eridi_0.2.0_x64-setup.exe", "nsis/Eridi_0.2.0_x64-setup.exe.sig",
           "msi/Eridi_0.2.0_x64_en-US.msi"]  # fmt: skip
    collect("x86_64-pc-windows-msvc", fake_bundle(tmp_path / "win", win), out, "0.2.0")
    linux = ["deb/Eridi_0.2.0_amd64.deb", "appimage/Eridi_0.2.0_amd64.AppImage",
             "appimage/Eridi_0.2.0_amd64.AppImage.sig"]  # fmt: skip
    collect("x86_64-unknown-linux-gnu", fake_bundle(tmp_path / "linux", linux), out, "0.2.0")
    assert {p.name for p in out.iterdir()} >= {
        "Eridi_0.2.0_aarch64.app.tar.gz", "Eridi_0.2.0_aarch64.app.tar.gz.sig",
        "Eridi_0.2.0_x64.app.tar.gz", "Eridi_0.2.0_x64_en-US.msi", "Eridi_0.2.0_amd64.deb",
    }  # fmt: skip

    now = datetime(2027, 1, 15, 9, 30, tzinfo=UTC)
    data = load_script("update_manifest").manifest(out, "0.2.0", "Fixes.", now)
    assert data["version"] == "0.2.0" and data["pub_date"] == "2027-01-15T09:30:00Z"
    assert set(data["platforms"]) == {"darwin-aarch64", "darwin-x86_64", "windows-x86_64",
                                      "linux-x86_64"}  # fmt: skip
    arm = data["platforms"]["darwin-aarch64"]
    assert arm["signature"] == "signature of Eridi.app.tar.gz.sig"
    assert arm["url"] == ("https://github.com/Hackmoses/eridi/releases/download/v0.2.0/"
                          "Eridi_0.2.0_aarch64.app.tar.gz")  # fmt: skip
    json.dumps(data)  # serializable as written


def test_manifest_needs_signed_bundles(tmp_path: Path) -> None:
    (tmp_path / "Eridi_0.2.0_amd64.deb").write_text("x", "utf-8")
    with pytest.raises(SystemExit, match="TAURI_SIGNING_PRIVATE_KEY"):
        load_script("update_manifest").manifest(tmp_path, "0.2.0")


def test_updater_points_at_github_releases_and_ships_without_a_key() -> None:
    conf = json.loads((Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" /
                       "tauri.conf.json").read_text("utf-8"))  # fmt: skip
    updater = conf["plugins"]["updater"]
    assert updater["endpoints"] == [
        "https://github.com/Hackmoses/eridi/releases/latest/download/latest.json"
    ]
    assert updater["pubkey"] == ""  # local builds never check; releases get the key
    assert conf["bundle"]["createUpdaterArtifacts"] is False
