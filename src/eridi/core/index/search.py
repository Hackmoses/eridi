"""One search over files and the record.

Results mix file hits and record hits, ranked by BM25. Record hits get a small boost,
since an explanation is usually what people are looking for. Files that contain every
word come first; if there are fewer than `limit` of those, files and entries that match
some of the words follow.

Snippets mark matched terms with `HIGHLIGHT_START` and `HIGHLIGHT_END` (control
characters that never occur in extracted text); use `SearchHit.segments()` or
`SearchHit.snippet_with()` to render them.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

HIGHLIGHT_START = "\x02"
HIGHLIGHT_END = "\x03"
RECORD_BOOST = 1.1
"""BM25 scores are negative (lower is better); multiplying moves record hits up."""
SNIPPET_TOKENS = 16
MIN_PREFIX_LENGTH = 3

_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
_WORDISH_RE = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True)
class SearchHit:
    kind: Literal["file", "entry"]
    id: str
    """For files, `<root_id>:<path>`; for entries, the entry ID."""
    title: str
    snippet: str
    score: float
    root_id: str | None = None
    root_label: str | None = None
    path: str | None = None
    entry_kind: str | None = None
    created_at: str | None = None
    author: str | None = None
    matched_all: bool = True

    def segments(self) -> list[tuple[str, bool]]:
        """The snippet as (text, highlighted) pieces."""
        out: list[tuple[str, bool]] = []
        for i, part in enumerate(re.split(f"[{HIGHLIGHT_START}{HIGHLIGHT_END}]", self.snippet)):
            if part:
                out.append((part, i % 2 == 1))
        return out

    def snippet_with(self, start: str, end: str) -> str:
        """The snippet with highlights wrapped in `start`/`end`, e.g. "[" and "]"."""
        return self.snippet.replace(HIGHLIGHT_START, start).replace(HIGHLIGHT_END, end)

    @property
    def plain_snippet(self) -> str:
        return self.snippet_with("", "")


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def parse_query(query: str) -> list[tuple[str, bool]]:
    """Split user input into (text, is_phrase) terms. Quotes make phrases; nothing else is
    special, so characters like `*`, `-`, `:` and `(` are just text."""
    terms: list[tuple[str, bool]] = []
    for phrase, word in _TOKEN_RE.findall(query):
        text, is_phrase = (phrase, True) if phrase else (word.strip('"'), False)
        if _WORDISH_RE.search(text):
            terms.append((text, is_phrase))
    return terms


def build_match(query: str, *, any_term: bool = False) -> str | None:
    """An FTS5 MATCH expression that is always valid, or None if there is nothing to find.

    The last bare word is matched as a prefix, so results appear while typing.
    """
    terms = parse_query(query)
    if not terms:
        return None
    parts = [_quote(text) for text, _ in terms]
    last_text, last_is_phrase = terms[-1]
    if not last_is_phrase and len(last_text) >= MIN_PREFIX_LENGTH:
        parts[-1] += "*"
    return (" OR " if any_term else " ").join(parts)


def _file_hits(
    conn: sqlite3.Connection, match: str, limit: int, project: str | None, matched_all: bool
) -> list[SearchHit]:
    project_filter = ""
    params: list[object] = [match]
    if project is not None:
        project_filter = (
            "AND EXISTS (SELECT 1 FROM project_roots pr WHERE pr.project_id = ? "
            "AND pr.root_id = f.root_id AND (pr.path_prefix = '' OR f.path = pr.path_prefix "
            "OR substr(f.path, 1, length(pr.path_prefix) + 1) = pr.path_prefix || '/'))"
        )
        params.append(project)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT f.root_id, f.path, f.title, r.label AS root_label,
               bm25(files_fts, 5.0, 10.0, 1.0) AS score,
               snippet(files_fts, 2, ?, ?, '…', {SNIPPET_TOKENS}) AS body_snip,
               snippet(files_fts, -1, ?, ?, '…', {SNIPPET_TOKENS}) AS snip
        FROM files_fts
        JOIN files f ON f.id = files_fts.rowid
        LEFT JOIN roots r ON r.id = f.root_id
        WHERE files_fts MATCH ? AND f.status = 'present' AND coalesce(r.search, 1) = 1
        {project_filter}
        ORDER BY score LIMIT ?
        """,  # noqa: S608 - only constant SQL fragments are interpolated
        [HIGHLIGHT_START, HIGHLIGHT_END, HIGHLIGHT_START, HIGHLIGHT_END, *params],
    ).fetchall()
    return [
        SearchHit(
            kind="file",
            id=f"{r['root_id']}:{r['path']}",
            title=r["title"] or r["path"].rsplit("/", 1)[-1],
            # Prefer a passage from the text; fall back to wherever the match is.
            snippet=(r["body_snip"] if HIGHLIGHT_START in (r["body_snip"] or "") else r["snip"])
            or "",
            score=float(r["score"]),
            root_id=r["root_id"],
            root_label=r["root_label"],
            path=r["path"],
            matched_all=matched_all,
        )
        for r in rows
    ]


def _entry_title(text: str, kind: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else kind.capitalize()
    return first if len(first) <= 80 else first[:79] + "…"


def _entry_hits(
    conn: sqlite3.Connection, match: str, limit: int, project: str | None, matched_all: bool
) -> list[SearchHit]:
    project_filter = "AND e.project_id = ?" if project is not None else ""
    params: list[object] = [match, *([project] if project is not None else []), limit]
    rows = conn.execute(
        f"""
        SELECT e.id, e.kind, e.created_at, e.author_name, e.text,
               bm25(entries_fts, 1.0, 2.0, 1.0) AS score,
               snippet(entries_fts, -1, ?, ?, '…', {SNIPPET_TOKENS}) AS snip,
               s.root_id, s.path, r.label AS root_label
        FROM entries_fts
        JOIN entries e ON e.seq = entries_fts.rowid
        LEFT JOIN entry_subjects s ON s.rowid = (
            SELECT min(rowid) FROM entry_subjects WHERE entry_id = e.id)
        LEFT JOIN roots r ON r.id = s.root_id
        WHERE entries_fts MATCH ? {project_filter}
        ORDER BY score LIMIT ?
        """,  # noqa: S608 - only constant SQL fragments are interpolated
        [HIGHLIGHT_START, HIGHLIGHT_END, *params],
    ).fetchall()
    return [
        SearchHit(
            kind="entry",
            id=r["id"],
            title=_entry_title(r["text"], r["kind"]),
            snippet=r["snip"] or "",
            score=float(r["score"]) * RECORD_BOOST,
            root_id=r["root_id"],
            root_label=r["root_label"],
            path=r["path"],
            entry_kind=r["kind"],
            created_at=r["created_at"],
            author=r["author_name"],
            matched_all=matched_all,
        )
        for r in rows
    ]


def _ranked(
    conn: sqlite3.Connection, match: str, limit: int, project: str | None, *, all_: bool
) -> list[SearchHit]:
    hits = _file_hits(conn, match, limit, project, all_) + _entry_hits(
        conn, match, limit, project, all_
    )
    hits.sort(key=lambda h: (h.score, h.kind != "entry", h.id))
    return hits[:limit]


def search(
    conn: sqlite3.Connection, query: str, limit: int = 20, project: str | None = None
) -> list[SearchHit]:
    """Search files and the record. Never raises on user input; empty input finds nothing."""
    match_all = build_match(query)
    if match_all is None or limit <= 0:
        return []
    hits = _ranked(conn, match_all, limit, project, all_=True)
    if len(hits) < limit and len(parse_query(query)) > 1:
        match_any = build_match(query, any_term=True) or match_all
        seen = {(h.kind, h.id) for h in hits}
        extra = [
            h
            for h in _ranked(conn, match_any, limit, project, all_=False)
            if (h.kind, h.id) not in seen
        ]
        hits += extra[: limit - len(hits)]
    return hits
