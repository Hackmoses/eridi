"""Write SHA256SUMS for every file in a release folder (the format `sha256sum -c` reads).

uv run python scripts/checksums.py dist/
"""

import hashlib
import sys
from pathlib import Path

NAME = "SHA256SUMS"


def checksums(folder: Path) -> str:
    lines = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.name != NAME):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.name}\n")
    return "".join(lines)


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    text = checksums(folder)
    (folder / NAME).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
