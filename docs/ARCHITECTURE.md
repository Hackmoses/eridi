# Architecture

## Principles

1. **Local-first, no file servers.** The app runs on the user's computer. Files stay where they
   are. The app keeps a disposable index and an append-only record. There is no server that
   holds user data. Hosting costs stay near zero as users grow.
2. **The record is sacred; the index is disposable.** See `RECORD_FORMAT.md`.
3. **One engine, many faces.** All logic lives in `core/`. The CLI, local API/UI and MCP server
   are thin layers that call the same engine functions.
4. **Connectors are plugins** behind one small interface. Adding a storage source never touches
   the engine.
5. **Cheapest answer first.** Structured query, then full-text search, then (later) a small
   local model, and a frontier model only when needed, given only the relevant records.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Fast to build; strong document-parsing libraries; agents write it well |
| Packaging and environments | `uv` | Fast, reproducible, one tool |
| Lint, format, types | `ruff`, `mypy --strict` | Catch agent mistakes early |
| Tests | `pytest`, `pytest-cov` | Standard |
| CLI | `typer` | Typed, simple |
| Local API | `fastapi` + `uvicorn` | Typed, serves the UI and JSON |
| UI | Static HTML/CSS/vanilla JS (optionally `htmx`) | No build step, nothing to maintain |
| Index | SQLite (standard library) with FTS5 | Zero-install, fast full-text search |
| Text extraction | `python-docx`, `openpyxl`, `python-pptx`, `pypdf`; stdlib for txt/md/csv/json/ipynb | Covers typical lab files |
| Diffs | `difflib` (stdlib) | Enough for text diffs |
| IDs | ULID (`python-ulid`) | Sortable, unique across devices |
| App directories | `platformdirs` | Correct paths on macOS and Windows |
| Config | TOML (`tomllib` to read, `tomli-w` to write) | Human-readable |
| Secrets | `keyring` | OS keychain on macOS and Windows |
| File watching (M9) | `watchdog` | Cross-platform |
| Microsoft (M10) | `msal` + plain HTTPS (`httpx`) to Microsoft Graph | Official auth library; Graph is simple REST |
| Google (M12) | `google-auth-oauthlib` + `httpx` to Drive API v3 | Official auth library |
| MCP (M6) | Official `mcp` Python SDK | Works with Claude and other MCP clients |
| Desktop packaging (M14) | PyInstaller-built engine + Tauri v2 shell | Small installers, auto-update, signing |

## Module map

```
src/rationale/
  __init__.py
  core/
    models.py        dataclasses: Root, FileInfo, TextVersion, Change, Entry, Project
    ids.py           ULID helpers, prefixed IDs (root_, proj_, dev_)
    paths.py         app-data locations (platformdirs), path normalization (POSIX, relative)
    config.py        load/save config.toml; user identity; roots; record folder; settings
    record/
      schema.py      validate and parse entries (v1); reading rules
      store.py       RecordStore: append(entry), iter_entries(), get(id); atomic writes
      views.py       resolve supersedes/tombstones; current view vs full history
    extract/
      base.py        Extractor protocol; registry by extension
      office.py      docx, xlsx, pptx
      pdf.py         pdf (flags "needs OCR" when no text)
      plain.py       txt, md, csv, json, ipynb, code files
      stubs.py       detect .gdoc/.gsheet stubs and cloud placeholders
    index/
      db.py          open/migrate SQLite; schema version table
      files.py       upsert files and text versions; FTS over file text
      records.py     load record entries into tables + FTS
      search.py      unified search over files and record; ranking; snippets
      rebuild.py     drop and rebuild from roots + record
    scan.py          walk roots via connectors; detect new/changed/removed; extract; snapshot
    snapshots.py     content-addressed gzipped text store; eviction
    changes.py       compute pending changes (changed files with no explaining note); diffs
    service.py       the engine's public API used by CLI, API and MCP (one class: Rationale)
  connectors/
    base.py          Connector protocol (below)
    local.py         local and synced folders
    msgraph.py       OneDrive/SharePoint (M10)
    gdrive.py        Google Drive (M12)
  cli/main.py        Typer app; every command calls service.py
  api/
    app.py           FastAPI app; token auth; serves ui/
    routes.py        JSON endpoints mirroring service.py
  ui/                index.html, app.js, app.css
  mcp/server.py      MCP tools mirroring service.py (read-only by default)
```

## Connector interface

```python
class Connector(Protocol):
    kind: str                                   # "local", "msgraph", "gdrive"
    def root_id(self) -> str: ...
    def list_files(self) -> Iterator[RemoteFile]: ...          # path, size, modified, content hash if known, is_placeholder
    def open_bytes(self, path: str) -> bytes: ...               # read-only
    def versions(self, path: str) -> list[RemoteVersion]: ...   # [] if the source keeps no history
    def open_version(self, path: str, version_id: str) -> bytes: ...
    def changes_since(self, cursor: str | None) -> tuple[list[RemoteChange], str]: ...  # delta; local returns full-scan based
    # From M11 only, and only if the user allowed saving to this root:
    def save(self, path: str, data: bytes) -> RemoteFile: ...
```

## Index schema (SQLite, rebuildable)

```
meta(key, value)                                  -- schema_version, last_full_scan
roots(id, kind, label, location, search, save, is_home_default)
files(id, root_id, path, size, modified, content_sha256, text_sha256, status, ext, placeholder, last_seen)
text_versions(file_id, text_sha256, first_seen, source)   -- source: "scan" or "provider-history"
files_fts(path, title, body)                      -- FTS5, external content = latest text
entries(id, kind, created_at, author_name, author_kind, project_id, outcome, text, supersedes, target_id, json)
entry_subjects(entry_id, root_id, path, text_sha256)
entries_fts(text, tags, summary)                  -- FTS5
projects(id, name, home_root_id)                  -- derived from `project` entries
project_roots(project_id, root_id, path_prefix)   -- folders that belong to a project
```

`rationale index rebuild` must produce the same search results as an incrementally maintained
index. A test enforces this.

## Change detection (v0)

1. Scan walks each root through its connector, respecting ignore rules (`.git`, `node_modules`,
   `~$*` Office temp files, `.DS_Store`, `Thumbs.db`, files over the size limit, and patterns in
   `.rationaleignore`).
2. A file is re-examined only if size or modified time changed. Then its bytes are hashed; if
   the hash changed, text is extracted and hashed.
3. If the text hash differs from the last seen version, the new text is snapshotted and a
   **pending change** is computed (derived, not stored in the record).
4. A pending change disappears once a record entry with a matching `change.to_text_sha256`
   exists (someone explained it), or the user dismisses it (stored locally in config, not the record).

## Local API security

A local web server on a laptop can be called by other programs and by malicious web pages
(DNS rebinding, cross-site requests). So:

- Bind only to `127.0.0.1` on a random free port. Write the port to `<app-data>/server.json`.
- Generate a random 32-byte token per install, stored via `keyring`. Every request needs it,
  as `Authorization: Bearer <token>` or a one-time URL the CLI opens (`/?t=<token>`) that sets an
  `HttpOnly`, `SameSite=Strict` cookie.
- Reject requests whose `Host` header isn't `127.0.0.1:<port>` or `localhost:<port>`.
- Reject cross-origin requests (`Origin` must match), and send no CORS headers.

## MCP server

`rationale mcp` runs an MCP server over stdio, so Claude Desktop, Claude Code and other MCP
clients can use the lab's record with no AI cost to the project.

Tools (read-only by default):
- `search(query, limit)` → files and record entries with snippets
- `file_history(path_or_id)` → versions, explaining notes, outcomes
- `get_entry(id)`, `list_recent(project?, kind?, limit)`
- `pending_changes(project?)`
- `show_diff(file, from?, to?)`

Write tool, only when the user turns on `mcp.allow_writes`:
- `add_note(file?, text, outcome?, project?)`, written with `author.kind = "agent"`, so
  people can always tell AI-written entries apart.

## Data locations

| What | Where | Synced? |
|---|---|---|
| Config | `<app-data>/config.toml` | No |
| Index | `<app-data>/index.sqlite` | No (disposable) |
| Snapshots | `<app-data>/snapshots/` | No, unless sharing is turned on |
| Record | User-chosen record folder | Yes, via the user's own cloud sync |
| Secrets | OS keychain | No |
| Logs | `<app-data>/logs/` (no document text) | No |

## Hosting (whole project)

| Need | Service | Cost |
|---|---|---|
| Code, issues, CI | GitHub (public repo) | Free |
| Release builds | GitHub Actions, macOS + Windows + Linux | Free for public repos |
| Downloads and updates | GitHub Releases | Free |
| Website | Cloudflare Pages or GitHub Pages | Free |
| Code signing (from M14) | Apple Developer Program; Azure Trusted Signing | ~$99/yr; ~$10/mo |

## License

Apache-2.0 for the code. Contributions accepted under the Developer Certificate of Origin
(signed-off commits). The name and logo are not covered by the license, so a future
"Rationale for Teams" or institution edition can be offered without confusion.
