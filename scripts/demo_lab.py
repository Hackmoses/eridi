"""Make demo lab folders for trying Eridi by hand (M8–M10 manual tests).

    uv run python scripts/demo_lab.py create ~/eridi-demo     # two demo storage folders
    uv run python scripts/demo_lab.py edit ~/eridi-demo       # change the Word protocol

`create` makes `Lab Drive/` (a realistic sample lab: Word protocol, Excel lot log, PDFs,
notes, an R script, a notebook) and `Shared Drive/` (imaging settings and an admin
folder), so a project can span two storage folders. Nothing outside the folder you name
is touched. `edit` changes one line of the protocol each time, like a labmate saving a new
version in Word.
"""

import argparse
import sys
from pathlib import Path

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from conftest import build_sample_lab

PROTOCOL = Path("Lab Drive") / "Protocols" / "Western_blot_blocking_v7.docx"
TIMES = ["2 h at RT", "overnight at 4 °C", "90 min at RT", "1 h at RT"]


def create(folder: Path) -> None:
    if (folder / "Lab Drive").exists():
        sys.exit(f"{folder / 'Lab Drive'} already exists. Choose another folder or delete it.")
    folder.mkdir(parents=True, exist_ok=True)
    build_sample_lab(folder)
    shared = folder / "Shared Drive"
    (shared / "Imaging").mkdir(parents=True)
    (shared / "Imaging" / "confocal_settings.md").write_text(
        "# Confocal settings\n\nLaser power 4% for the antibody validation stains.\n",
        encoding="utf-8",
    )
    (shared / "Admin").mkdir()
    (shared / "Admin" / "budget_2027.md").write_text(
        "# Budget 2027\n\nAntibody budget: new lots of anti-pERK and anti-GAPDH.\n",
        encoding="utf-8",
    )
    print(f"Created {folder / 'Lab Drive'} and {shared}")


def edit(folder: Path) -> None:
    path = folder / PROTOCOL
    doc = Document(str(path))
    line = doc.paragraphs[2]
    current = line.text.removeprefix("Primary antibody incubation: ").rstrip(".")
    new = TIMES[(TIMES.index(current) + 1) % len(TIMES)] if current in TIMES else TIMES[0]
    line.text = f"Primary antibody incubation: {new}."
    doc.save(str(path))
    print(f"Edited {path}: incubation {current} → {new}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["create", "edit"])
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve()
    create(folder) if args.action == "create" else edit(folder)


if __name__ == "__main__":
    main()
