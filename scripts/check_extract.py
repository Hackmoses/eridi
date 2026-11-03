"""Human check for M2: list a folder and show what Eridi extracts from each file.

    uv run python scripts/check_extract.py "<lab folder>" [--show-text]

Read-only. Prints status, title and path for every file, then a summary.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from eridi.connectors.local import LocalConnector
from eridi.core.config import Settings
from eridi.core.scan import read_and_extract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--show-text", action="store_true", help="print the first 200 characters")
    args = parser.parse_args()
    settings = Settings()
    connector = LocalConnector(args.folder, "root_CHECK")
    statuses: Counter[str] = Counter()
    problems: list[tuple[str, str]] = []
    for remote in connector.list_files():
        _, result = read_and_extract(
            connector, remote, settings.size_limit_bytes, read_placeholders=False
        )
        statuses[result.status] += 1
        flag = " (online-only)" if remote.is_placeholder else ""
        print(f"{result.status:<15} {result.title or '':<45.45} {remote.path}{flag}")
        if args.show_text and result.text:
            print("    " + result.text[:200].replace("\n", " / "))
        if result.status == "error":
            problems.append((remote.path, result.reason or ""))
    print("\nSummary: " + ", ".join(f"{n} {s}" for s, n in statuses.most_common()))
    for path, reason in problems:
        print(f"  error: {path}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
