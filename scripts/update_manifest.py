"""Write `latest.json`, the manifest the desktop app's updater reads, from a folder of
collected release files (see collect_bundles.py) and their `.sig` signatures.

    uv run python scripts/update_manifest.py <dir> <version> [--notes TEXT]

Each platform points at its update bundle on the GitHub release for `v<version>`.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO = "Hackmoses/eridi"


def platform_of(name: str) -> str | None:
    if name.endswith("_aarch64.app.tar.gz"):
        return "darwin-aarch64"
    if name.endswith("_x64.app.tar.gz"):
        return "darwin-x86_64"
    if name.endswith("-setup.exe"):
        return "windows-x86_64"
    if name.endswith(".AppImage"):
        return "linux-x86_64"
    return None


def manifest(
    folder: Path, version: str, notes: str = "", now: datetime | None = None
) -> dict[str, Any]:
    platforms: dict[str, dict[str, str]] = {}
    for sig in sorted(folder.glob("*.sig")):
        bundle = sig.with_suffix("")  # drop ".sig"
        platform = platform_of(bundle.name)
        if platform is None or not bundle.exists():
            continue
        url = f"https://github.com/{REPO}/releases/download/v{version}/{quote(bundle.name)}"
        platforms[platform] = {"signature": sig.read_text("utf-8").strip(), "url": url}
    if not platforms:
        raise SystemExit(f"No signed update bundles in {folder}. Is TAURI_SIGNING_PRIVATE_KEY set?")
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": version, "notes": notes, "pub_date": stamp, "platforms": platforms}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("version")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    data = manifest(args.folder, args.version.removeprefix("v"), args.notes)
    (args.folder / "latest.json").write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    print(json.dumps(data, indent=2))
