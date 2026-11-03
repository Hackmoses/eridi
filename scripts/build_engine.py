"""Build the packaged engine with PyInstaller and name it for the desktop app.

    uv run python scripts/build_engine.py

Output: `desktop/src-tauri/binaries/eridi-engine-<target triple>[.exe]`, the name Tauri
expects for a sidecar (`externalBin`). The target triple comes from `rustc -vV`, or the
`ERIDI_TARGET_TRIPLE` environment variable (set it when cross-building, e.g. for
`aarch64-apple-darwin` on an Intel Mac).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
OUT = ROOT / "desktop" / "src-tauri" / "binaries"


def target_triple() -> str:
    explicit = os.environ.get("ERIDI_TARGET_TRIPLE")
    if explicit:
        return explicit
    try:
        info = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        sys.exit("Couldn't run `rustc -vV`. Install Rust, or set ERIDI_TARGET_TRIPLE.")
    for line in info.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    sys.exit("`rustc -vV` didn't say the host target. Set ERIDI_TARGET_TRIPLE.")


def main() -> Path:
    triple = target_triple()
    build = ROOT / "build" / "engine"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(build / "dist"), "--workpath", str(build / "work"),
            str(PACKAGING / "eridi-engine.spec"),
        ],
        check=True,
        cwd=PACKAGING,
    )  # fmt: skip
    suffix = ".exe" if sys.platform == "win32" else ""
    built = build / "dist" / f"eridi-engine{suffix}"
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"eridi-engine-{triple}{suffix}"
    shutil.copy2(built, target)
    print(target)
    return target


if __name__ == "__main__":
    main()
