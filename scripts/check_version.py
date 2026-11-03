"""Check that a release tag matches the version everywhere it is written.

uv run python scripts/check_version.py v0.6.0
"""

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def versions() -> dict[str, str]:
    desktop = ROOT / "desktop"
    return {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"][
            "version"
        ],
        "desktop/package.json": json.loads((desktop / "package.json").read_text("utf-8"))[
            "version"
        ],
        "desktop/src-tauri/Cargo.toml": tomllib.loads(
            (desktop / "src-tauri" / "Cargo.toml").read_text("utf-8")
        )["package"]["version"],
        "desktop/src-tauri/tauri.conf.json": json.loads(
            (desktop / "src-tauri" / "tauri.conf.json").read_text("utf-8")
        )["version"],
    }


def problems(tag: str) -> list[str]:
    if not tag.startswith("v"):
        return [f"Release tags look like v1.2.3, not {tag!r}."]
    wanted = tag[1:]
    return [
        f"{where} says {found}, but the tag is {tag}. Update it, commit, and tag again."
        for where, found in versions().items()
        if found != wanted
    ]


if __name__ == "__main__":
    found = problems(sys.argv[1])
    for problem in found:
        print(problem)
    sys.exit(1 if found else 0)
