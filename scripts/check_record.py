"""Human check for M1: write and read record entries in a (synced) record folder.

    uv run python scripts/check_record.py write "<record folder>" --device "Mac" --count 5
    uv run python scripts/check_record.py read "<record folder>"

Run `write` on two computers against the same synced folder, wait for sync, then `read`
on both. Writes only inside the record folder you name.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from eridi.core.models import Author
from eridi.core.record.schema import make_entry
from eridi.core.record.store import RecordStore


def write(folder: Path, device: str, count: int) -> None:
    store = RecordStore.init(folder)
    author = Author(f"Record check ({device})", "human")
    for i in range(1, count + 1):
        e = make_entry("note", author, f"dev_CHECK_{device}", f"Record check {i} from {device}.")
        store.append(e)
        print(f"wrote {e.id}")
    print(f"\n{count} entries written to {folder}")


def read(folder: Path) -> int:
    result = RecordStore(folder).read()
    by_device = Counter(e.device_id for e in result.entries)
    print(f"Record folder: {folder}")
    print(f"Entries: {len(result.entries)}")
    for device, n in sorted(by_device.items()):
        print(f"  {device}: {n}")
    if result.newer_version:
        print(f"Written by a newer version (skipped): {len(result.newer_version)}")
    if result.problems:
        print(f"\nProblems ({len(result.problems)}), e.g. sync conflict copies:")
        for p in result.problems:
            print(f"  {p.path}: {p.reason}")
        return 1
    print("No problems: no conflict copies, no unreadable entries.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write")
    w.add_argument("folder", type=Path)
    w.add_argument("--device", required=True, help='a short name for this computer, e.g. "Mac"')
    w.add_argument("--count", type=int, default=5)
    r = sub.add_parser("read")
    r.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "write":
        write(args.folder, args.device, args.count)
        return 0
    return read(args.folder)


if __name__ == "__main__":
    sys.exit(main())
