"""Smoke-test an engine: start it as the desktop app does, call its health endpoint, run a
parallel scan, and check it stops when the app goes away.

    uv run python scripts/smoke_engine.py desktop/src-tauri/binaries/eridi-engine-<triple>
    uv run python scripts/smoke_engine.py            # the engine from this source tree

Uses a throwaway Eridi home and no keychain. Exits non-zero with a reason on failure.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_S = 90.0


def make_pdf(page_streams: list[bytes]) -> bytes:
    """Build a minimal valid PDF. Each stream draws one page."""
    objects: list[bytes] = []
    n_pages = len(page_streams)
    first_page = 3
    font_obj = first_page + 2 * n_pages
    kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, stream in enumerate(page_streams):
        content_obj = first_page + 2 * i + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def engine_command(binary: str | None) -> list[str]:
    return [binary] if binary else [sys.executable, "-m", "eridi"]


def environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["ERIDI_HOME"] = str(home)
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"  # never the real keychain
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def read_json_line(proc: "subprocess.Popen[str]", timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def read() -> None:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if line:
            result.update(json.loads(line))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if not result:
        raise SystemExit(f"The engine said nothing on stdout within {timeout:.0f} s.")
    return result


def run(command: list[str], env: dict[str, str], *args: str) -> str:
    done = subprocess.run([*command, *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=TIMEOUT_S, check=False)  # fmt: skip
    if done.returncode != 0:
        raise SystemExit(f"`eridi {' '.join(args)}` failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def smoke(binary: str | None = None) -> dict[str, Any]:
    command = engine_command(binary)
    with tempfile.TemporaryDirectory() as tmp:
        home, lab = Path(tmp) / "home", Path(tmp) / "lab"
        env = environment(home)
        version = run(command, env, "version").strip()
        print(version)

        # A parallel scan: in a packaged engine, workers start by re-running the executable.
        lab.mkdir()
        for i in range(40):
            (lab / f"note_{i:02d}.md").write_text(f"# Note {i}\n\nBlocking buffer {i}.\n", "utf-8")
        (lab / "western_blot.pdf").write_bytes(
            make_pdf([b"BT /F1 12 Tf 72 720 Td (Transfer at 100 V for 90 minutes) Tj ET"])
        )
        run(command, env, "init", "--name", "Smoke Test", "--email", "smoke@example.org",
            "--record-folder", str(home / "record"))  # fmt: skip
        run(command, env, "storage", "add", str(lab), "--label", "Lab", "--no-mark")
        (home / "config.toml").write_text(
            (home / "config.toml")
            .read_text("utf-8")
            .replace("scan_workers = 0", "scan_workers = 2"),
            "utf-8",
        )
        scan = run(command, env, "scan", "--details")
        if "41" not in scan or "with 2 workers" not in scan:
            raise SystemExit(f"The scan didn't read 41 files with 2 workers:\n{scan}")
        if "western_blot.pdf" not in run(command, env, "search", "transfer minutes"):
            raise SystemExit("Search didn't find text in a PDF (is PDFium in the bundle?).")
        if "note_07.md" not in run(command, env, "search", "buffer 7"):
            raise SystemExit("Search didn't find a scanned file.")

        # As the desktop app runs it: JSON lines on stdout, commands on stdin.
        proc = subprocess.Popen([*command, "serve", "--sidecar"], env=env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # fmt: skip
        try:
            ready = read_json_line(proc, TIMEOUT_S)
            if ready.get("event") != "ready":
                raise SystemExit(f"Expected a ready event, got {ready}")
            port = ready["port"]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as r:
                health = json.loads(r.read())
            if not health.get("ok") or health.get("version") != ready["version"]:
                raise SystemExit(f"Unexpected health answer: {health}")
            assert proc.stdin is not None
            proc.stdin.write("link\n")
            proc.stdin.flush()
            link = read_json_line(proc, 10)
            if not str(link.get("url", "")).startswith(f"http://127.0.0.1:{port}/?t="):
                raise SystemExit(f"Expected a sign-in link, got {link}")
            started = time.monotonic()
            proc.stdin.close()  # the desktop app quit: the engine must stop by itself
            proc.wait(timeout=30)
            print(f"Health OK ({health['version']}); stopped {time.monotonic() - started:.1f} s "
                  "after the app closed its end.")  # fmt: skip
            return health
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    smoke(sys.argv[1] if len(sys.argv) > 1 else None)
