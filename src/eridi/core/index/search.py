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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from eridi.core.index.projects import entry_in_project_sql, file_in_project_sql

if TYPE_CHECKING:
    from eridi.core.changes import FileStats

HIGHLIGHT_START = "\x02"
HIGHLIGHT_END = "\x03"
RECORD_BOOST = 1.1
"""BM25 scores are negative (lower is better); multiplying moves record hits up."""
SNIPPET_TOKENS = 16
MIN_PREFIX_LENGTH = 3

_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
_WORDISH_RE = re.compile(r"\w", re.UNICODE)


Match = Literal["exact", "near", "all", "some"]
MATCH_ORDER: tuple[Match, ...] = ("exact", "near", "all", "some")
"""exact: the words as a phrase, in order. near: all the words close together, any order.
all: all the words anywhere. some: only one of the words, rarer words first."""
NEAR_DISTANCE = 10


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
    match: Match = "all"
    """How it matched; results come in this order (see `MATCH_ORDER`)."""
    term: str | None = None
    """For `match == "some"`: the one search word it contains."""
    stats: "FileStats | None" = None
    """For files: versions, notes, last edit and whether a why is needed (set by Eridi)."""

    @property
    def matched_all(self) -> bool:
        """It contains every search word (exactly, close together, or anywhere)."""
        return self.match != "some"

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


_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SUFFIXES = ("ational", "ations", "ation", "ings", "ing", "ions", "ion", "ies", "es", "ed", "s")
SNIPPET_CHARS = 160


def word_stem(word: str) -> str:
    """A rough stem so a highlight finds other forms: blocking → block, proteins → protein."""
    w = word.casefold()
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def make_snippet(text: str, words: list[str], phrase: bool = False, *, whole: bool = False) -> str:
    """A short passage around the best match, with matches wrapped in highlight marks.

    Built here rather than with SQLite's snippet(), which re-reads the whole document and
    made searches for common words slow on large PDFs. Returns "" if nothing matches.
    """
    if not words or not text:
        return ""
    stems = sorted({word_stem(w) for w in words}, key=len, reverse=True)
    term = re.compile(r"\b(?:" + "|".join(re.escape(st) for st in stems) + r")\w*", re.I)
    hit = None
    if phrase and len(words) > 1:
        joined = r"\W+".join(re.escape(word_stem(w)) + r"\w*" for w in words)
        hit = re.search(r"\b" + joined, text, re.I)
    if hit is None:
        best, best_count = None, 0
        for n, m in enumerate(term.finditer(text)):
            if n >= 50:
                break
            window = text[m.start() : m.start() + SNIPPET_CHARS]
            count = len({word_stem(x.group(0)) for x in term.finditer(window)})
            if count > best_count:
                best, best_count = m, count
                if count == len(stems):
                    break
        hit = best
    if hit is None:
        return ""
    if whole:
        start, end = 0, len(text)
    else:
        start = max(0, hit.start() - SNIPPET_CHARS // 4)
        end = min(len(text), start + SNIPPET_CHARS)
        if start > 0:
            space = text.find(" ", start)
            start = space + 1 if 0 <= space < hit.start() else start
        if end < len(text):
            space = text.rfind(" ", hit.end(), end)
            end = space if space > hit.end() else end
    window = text[start:end]
    if phrase and len(words) > 1 and hit.re.pattern != term.pattern:
        marked = re.sub(hit.re.pattern, lambda m: HIGHLIGHT_START + m.group(0) + HIGHLIGHT_END,
                        window, flags=re.I)  # fmt: skip
    else:
        marked = term.sub(lambda m: HIGHLIGHT_START + m.group(0) + HIGHLIGHT_END, window)
    return ("…" if start > 0 else "") + marked + ("…" if end < len(text) else "")


def _opening(text: str) -> str:
    """The start of a text, for matches the highlighter can't place (e.g. accents typed
    differently): better a passage without marks than none."""
    cut = text[:SNIPPET_CHARS]
    return cut + ("…" if len(text) > SNIPPET_CHARS else "")


def group_label(hit: SearchHit) -> str:
    """A short heading for a result's group, e.g. "Exact phrase" or "Only “myelin”"."""
    if hit.match == "exact":
        return "Exact phrase"
    if hit.match == "near":
        return "Words close together"
    if hit.match == "all":
        return "All words"
    return f"Only “{hit.term}”"


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
    conn: sqlite3.Connection,
    expr: str,
    limit: int,
    project: str | None,
    how: Match,
    words: list[str],
    phrase: bool,
) -> list[SearchHit]:
    project_filter = ""
    params: list[object] = [expr]
    if project is not None:
        project_filter = "AND " + file_in_project_sql("f")
        params.append(project)
    params.append(limit)
    # Rank first, then build snippets only for the rows shown: snippet() on every match of a
    # common word is what made broad searches slow.
    rows = conn.execute(
        f"""
        WITH top AS (
            SELECT files_fts.rowid AS id, bm25(files_fts, 5.0, 10.0, 1.0) AS score
            FROM files_fts
            JOIN files f ON f.id = files_fts.rowid
            LEFT JOIN roots r ON r.id = f.root_id
            WHERE files_fts MATCH ? AND f.status = 'present' AND coalesce(r.search, 1) = 1
            {project_filter}
            ORDER BY score LIMIT ?
        )
        SELECT f.root_id, f.path, f.title, r.label AS root_label, top.score AS score,
               files_fts.body AS body
        FROM files_fts
        JOIN top ON top.id = files_fts.rowid
        JOIN files f ON f.id = files_fts.rowid
        LEFT JOIN roots r ON r.id = f.root_id
        WHERE files_fts MATCH ? AND files_fts.rowid IN (SELECT id FROM top)
        ORDER BY top.score
        """,  # noqa: S608 - only constant SQL fragments are interpolated
        [*params, expr],
    ).fetchall()
    return [
        SearchHit(
            kind="file",
            id=f"{r['root_id']}:{r['path']}",
            title=r["title"] or r["path"].rsplit("/", 1)[-1],
            # A passage from the text; if the match is only in the name, the path instead.
            snippet=make_snippet(r["body"] or "", words, phrase)
            or make_snippet(r["path"], words, phrase, whole=True)
            or _opening(r["body"] or ""),
            score=float(r["score"]),
            root_id=r["root_id"],
            root_label=r["root_label"],
            path=r["path"],
            match=how,
        )
        for r in rows
    ]


def _entry_title(text: str, kind: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else kind.capitalize()
    return first if len(first) <= 80 else first[:79] + "…"


def _entry_hits(
    conn: sqlite3.Connection, expr: str, limit: int, project: str | None, how: Match
) -> list[SearchHit]:
    project_filter = "AND " + entry_in_project_sql("e") if project is not None else ""
    params: list[object] = [expr, *([project, project] if project is not None else []), limit]
    rows = conn.execute(
        f"""
        WITH top AS (
            SELECT entries_fts.rowid AS seq, bm25(entries_fts, 1.0, 2.0, 1.0) AS score
            FROM entries_fts
            JOIN entries e ON e.seq = entries_fts.rowid
            WHERE entries_fts MATCH ? {project_filter}
            ORDER BY score LIMIT ?
        )
        SELECT e.id, e.kind, e.created_at, e.author_name, e.text, top.score AS score,
               snippet(entries_fts, -1, ?, ?, '…', {SNIPPET_TOKENS}) AS snip,
               s.root_id, s.path, r.label AS root_label
        FROM entries_fts
        JOIN top ON top.seq = entries_fts.rowid
        JOIN entries e ON e.seq = entries_fts.rowid
        LEFT JOIN entry_subjects s ON s.rowid = (
            SELECT min(rowid) FROM entry_subjects WHERE entry_id = e.id)
        LEFT JOIN roots r ON r.id = s.root_id
        WHERE entries_fts MATCH ? AND entries_fts.rowid IN (SELECT seq FROM top)
        ORDER BY top.score
        """,  # noqa: S608 - only constant SQL fragments are interpolated
        [*params, HIGHLIGHT_START, HIGHLIGHT_END, expr],
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
            match=how,
        )
        for r in rows
    ]


def _ranked(
    conn: sqlite3.Connection,
    expr: str,
    limit: int,
    project: str | None,
    how: Match,
    words: list[str],
) -> list[SearchHit]:
    hits = _file_hits(conn, expr, limit, project, how, words, how == "exact") + _entry_hits(
        conn, expr, limit, project, how
    )
    hits.sort(key=lambda h: (h.score, h.kind != "entry", h.id))
    return hits[:limit]


def _term_expr(text: str, prefix: bool) -> str:
    return _quote(text) + ("*" if prefix else "")


def _rarity(conn: sqlite3.Connection, expr: str) -> int:
    """How many files and entries contain a term (fewer = more specific)."""
    total = 0
    for table in ("files_fts", "entries_fts"):
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",  # noqa: S608 - fixed names
            (expr,),
        ).fetchone()
        total += int(row[0])
    return total


def match_tiers(conn: sqlite3.Connection, query: str) -> list[tuple[Match, str, str | None]]:
    """The FTS queries to run, best match first: (how, expression, single term)."""
    terms = parse_query(query)
    if not terms:
        return []
    last_text, last_is_phrase = terms[-1]
    prefix = [False] * len(terms)
    prefix[-1] = not last_is_phrase and len(last_text) >= MIN_PREFIX_LENGTH
    parts = [_term_expr(text, p) for (text, _), p in zip(terms, prefix, strict=True)]
    tiers: list[tuple[Match, str, str | None]] = []
    if len(terms) >= 2:
        phrase = " ".join(text for text, _ in terms)
        tiers.append(("exact", _term_expr(phrase, prefix[-1]), None))
        tiers.append(("near", f"NEAR({' '.join(parts)}, {NEAR_DISTANCE})", None))
    tiers.append(("all", " ".join(parts), None))
    if len(terms) >= 2:
        singles = sorted(
            enumerate(zip(terms, parts, strict=True)),
            key=lambda item: (_rarity(conn, item[1][1]), item[0]),
        )
        tiers += [("some", part, text) for _, ((text, _), part) in singles]
    return tiers


def search(
    conn: sqlite3.Connection, query: str, limit: int = 20, project: str | None = None
) -> list[SearchHit]:
    """Search files and the record. Never raises on user input; empty input finds nothing.

    Results come in tiers: the exact phrase, then all the words close together, then all
    the words anywhere, then each single word on its own, rarest word first. Within a tier,
    results are ranked by relevance (BM25), with record entries slightly ahead of files.
    """
    if limit <= 0:
        return []
    hits: list[SearchHit] = []
    seen: set[tuple[str, str]] = set()
    words = [w for text, _ in parse_query(query) for w in _WORD_RE.findall(text)]
    for how, expr, term in match_tiers(conn, query):
        if len(hits) >= limit:
            break
        tier_words = _WORD_RE.findall(term) if term else words
        for hit in _ranked(conn, expr, limit, project, how, tier_words):
            key = (hit.kind, hit.id)
            if key in seen:
                continue
            seen.add(key)
            hits.append(replace(hit, term=term) if term else hit)
            if len(hits) >= limit:
                break
    return hits
