"""Gather one target's installers and update bundles into a folder, with names that are
unique across targets (both Macs build a file called `Eridi.app.tar.gz`).

    uv run python scripts/collect_bundles.py <target triple> <bundle dir> <out dir>

<bundle dir> is `desktop/src-tauri/target/<triple>/release/bundle`.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = ("dmg/*.dmg", "macos/*.app.tar.gz", "macos/*.app.tar.gz.sig", "nsis/*-setup.exe",
            "nsis/*-setup.exe.sig", "msi/*.msi", "deb/*.deb", "appimage/*.AppImage",
            "appimage/*.AppImage.sig")  # fmt: skip
ARCH = {"aarch64-apple-darwin": "aarch64", "x86_64-apple-darwin": "x64"}


def version() -> str:
    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text("utf-8"))
    return str(conf["version"])


def collect(target: str, bundle: Path, out: Path, app_version: str | None = None) -> list[Path]:
    app_version = app_version or version()
    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for pattern in PATTERNS:
        for source in sorted(bundle.glob(pattern)):
            name = source.name
            if ".app.tar.gz" in name:  # e.g. Eridi.app.tar.gz → Eridi_0.1.0_aarch64.app.tar.gz
                stem = name.split(".app.tar.gz")[0]
                name = (
                    f"{stem}_{app_version}_{ARCH[target]}.app.tar.gz" + name.split(".app.tar.gz")[1]
                )
            target_path = out / name.replace(" ", "_")
            shutil.copy2(source, target_path)
            copied.append(target_path)
    if not copied:
        sys.exit(f"No installers found in {bundle}.")
    return copied


if __name__ == "__main__":
    for path in collect(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])):
        print(path)
