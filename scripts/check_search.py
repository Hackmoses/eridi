"""Try M3 search on a real folder (and optionally a record folder), without saving anything.

    uv run python scripts/check_search.py "<lab folder>" "blocking buffer" ["more queries" ...]
    uv run python scripts/check_search.py "<lab folder>" "query" --record "<record folder>"

Builds a throwaway in-memory index; nothing is written to disk.
"""

import argparse
import sys
import time
from pathlib import Path

from eridi.core.config import Settings
from eridi.core.index.db import open_index
from eridi.core.index.rebuild import rebuild
from eridi.core.index.search import search
from eridi.core.models import Root
from eridi.core.record.store import RecordStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("queries", nargs="+")
    parser.add_argument("--record", type=Path, help="an existing record folder to include")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    conn = open_index(Path(":memory:"))
    root = Root("root_CHECK", args.folder.name, str(args.folder))
    store = RecordStore(args.record) if args.record else None
    start = time.perf_counter()
    report = rebuild(conn, [root], store, Settings())
    ingest = report.roots.get(root.id)
    print(f"Indexed {ingest.indexed if ingest else 0} files in {time.perf_counter() - start:.1f} s")
    for q in args.queries:
        start = time.perf_counter()
        hits = search(conn, q, args.limit)
        ms = (time.perf_counter() - start) * 1000
        print(f"\n{q!r}: {len(hits)} hits in {ms:.0f} ms")
        for h in hits:
            where = h.path or ""
            mark = "" if h.matched_all else " (some words)"
            print(f"  [{h.kind}] {h.title}  {where}{mark}")
            print(f"      {h.snippet_with('[', ']').replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
