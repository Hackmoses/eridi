# Implementation plan

Milestones run in order. Each one ships something usable and testable. Agents: do one
milestone at a time, follow `AGENTS.md`, and stop at each **Human check**.

Each milestone has:
- **Goal**: what a person can do when it's finished
- **Tasks**: numbered, in order, each with its tests
- **Human check**: what a person tries by hand before the next milestone starts
- **Done when**: the exit criteria

| Phase | Milestones | Target | Product version |
|---|---|---|---|
| A. Find it, see what changed, say why | M0–M7 | Nov 2026 | v0 |
| B. Projects, teammates, Microsoft | M8–M10 | Dec 2026 | v0.5 |
| C. Saving, Google, outcomes, installers | M11–M14 | Jan–Feb 2027 | v0.6 → v1 (pilot labs) |
| D. Ask, AI-assistant access, owned model | M15–M17 | Mar 2027 → | v2 → v4 |

The read-only MCP server moves earlier than in the vision brief (M6 instead of v3). It's cheap
once search exists, and it lets people ask Claude about their lab's record in v0, at no AI cost
to the project.

---

## Phase A — v0: find it, see what changed, say why

### M0. Foundations

**Goal:** an empty but working project that builds and tests on macOS, Windows and Linux.

Tasks:
1. **Scaffold the package** with `uv init --package` → `src/eridi/`, `pyproject.toml`
   (Python ≥3.12, hatchling), console script `eridi = "eridi.cli.main:app"`.
   *Test:* `uv run python -c "import eridi"` works; `uv run eridi --help` prints help.
2. **Tooling:** ruff (lint + format), mypy strict on `src/`, pytest with coverage. Add
   `pre-commit` config running ruff.
   *Test:* the full check from AGENTS.md passes on the empty project.
3. **CI** (`.github/workflows/ci.yml`): matrix `ubuntu-latest`, `macos-latest`,
   `windows-latest` × Python 3.12, 3.13. Steps: `uv sync`, ruff, mypy, pytest.
   *Test:* CI is green on all six jobs.
4. **Open-source files:** `LICENSE` (Apache-2.0 full text), `README.md` (what it is, status:
   pre-alpha, how to run), `CONTRIBUTING.md` (DCO sign-off, how to run checks),
   `SECURITY.md` (how to report issues privately), `CODE_OF_CONDUCT.md` (Contributor Covenant),
   issue templates for bug and feature.
5. **Core helpers:** `core/ids.py` (ULIDs, prefixed IDs), `core/paths.py` (app-data dir via
   platformdirs, overridable with `ERIDI_HOME` env var for tests; `to_posix_relative(root, path)`).
   *Tests:* IDs sort by creation time and are unique over 10,000 generations; path
   normalization turns `Protocols\WB\v7.docx` on Windows into `Protocols/WB/v7.docx`; Unicode
   and spaces in names round-trip; paths outside the root raise a clear error.
6. **Test fixture builder** `tests/conftest.py`: a `sample_lab(tmp_path)` fixture that creates a
   realistic lab folder at test time, with no binary files committed: a `.docx` protocol (via
   python-docx), an `.xlsx` antibody lot log, a text-based PDF, a PDF with no text layer,
   `.md` notes, an `.R` script, an `.ipynb`, a `.gdoc` stub, an Office temp file `~$v7.docx`,
   and a nested folder with a Unicode name (`Protokolle_Ü/`).

**Human check:** clone on your Mac and a Windows machine, run `uv sync && uv run pytest`.
**Done when:** CI green on all platforms; open-source files present.

### M1. Record store (the part that must never break)

**Goal:** entries can be written and read safely, from multiple devices at once, inside a
sync folder.

Tasks:
1. **Models and schema** (`core/models.py`, `core/record/schema.py`) exactly per
   `RECORD_FORMAT.md`. `parse_entry(dict) -> Entry` validates required fields and keeps unknown
   ones in `extra_unknown`. `serialize_entry(Entry) -> str` writes sorted keys, 2-space indent,
   trailing newline.
   *Tests:* round-trip equality; every required field missing → clear error naming the field;
   unknown fields preserved through round-trip; fixtures in `tests/fixtures/record/v1/` (one per
   kind) parse correctly.
2. **RecordStore** (`core/record/store.py`): `init(folder)` writes `eridi-record.json`;
   `append(entry)` writes atomically into `entries/YYYY/MM/<id>.json` (temp file, fsync, rename);
   `iter_entries()` yields all valid entries sorted by ID; `get(id)`.
   *Tests:* append then read; a crash simulated between temp write and rename leaves no visible
   entry; files named `<ulid> (1).json` or `<ulid>-DESKTOP-X.json` are ignored and reported;
   invalid JSON is skipped and reported; an entry with schema `eridi.record/2` is skipped
   with a single warning.
3. **Concurrent writers test:** two `RecordStore` instances (simulating two laptops) append 500
   entries each to the same folder from separate processes. All 1,000 are readable, none lost
   or corrupted.
4. **Views** (`core/record/views.py`): `current(entries)` applies `supersedes` (newest wins)
   and `tombstone` (hide target); `history(entries, subject)` returns everything about a file,
   including superseded and retracted entries, labeled as such.
   *Tests:* supersede chains of length 3; a tombstone of a superseded entry; a tombstone of a
   missing ID is harmless.
5. **Config** (`core/config.py`): `config.toml` with user identity (name, email), device ID,
   record folder path, roots list, settings (size limit, snapshot cap). Load, save, defaults.
   *Tests:* missing config → defaults; invalid TOML → clear error that names the file and line.

**Human check:** point the record folder at a folder inside your real OneDrive or Google Drive
sync folder. Write a few entries with a short script on two computers. Confirm both computers
see all entries and nothing turns into conflict copies.
**Done when:** all tests pass on all platforms, including the concurrency test.

### M2. Local connector and text extraction

**Goal:** the app can list a folder's files and read their text.

Tasks:
1. **Connector protocol** (`connectors/base.py`) per ARCHITECTURE.md, plus `RemoteFile` and
   `RemoteVersion` dataclasses.
2. **Local connector** (`connectors/local.py`): walks a folder, applies ignore rules (defaults
   plus `.eridiignore` with gitignore-style patterns via `pathspec`), skips symlinks that
   leave the root, returns size/mtime, opens files read-only. `versions()` returns `[]`.
   Detects cloud placeholders where possible: on Windows check
   `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` (0x400000) and `FILE_ATTRIBUTE_OFFLINE`; on macOS,
   files with zero allocated blocks but nonzero size. Placeholders are listed but not read
   unless the setting `read_online_only_files` is on, so a scan never downloads a user's whole
   cloud drive.
   *Tests:* ignore rules (defaults and custom); Office temp files skipped; Unicode paths;
   unreadable file → logged and skipped, scan continues; a symlink out of the root is not
   followed. The connector never opens a file for writing (assert via a patched `open`).
3. **Extractors** (`core/extract/`), registered by extension: docx (paragraphs and tables),
   xlsx (sheet names and cell values, capped at 20,000 cells), pptx (slide text), pdf (pypdf;
   if under 20 characters of text per page on average → mark `needs_ocr`), txt/md/csv/json,
   code files (`.py .R .m .jl .sh .sql`), ipynb (markdown and code cells, no outputs).
   `.gdoc`/`.gsheet`/`.gslides` → mark `cloud_document` (handled in M12). Unknown types →
   index the file name and path only.
   Each extractor returns `ExtractResult(text, title, status)` and never raises; failures return
   `status="error"` with a reason.
   *Tests:* each sample_lab file extracts the expected key phrases; a corrupted docx returns
   `status="error"` instead of crashing; extraction of a 50 MB file is skipped by the size limit.

**Human check:** run a small script that lists your real lab folder and prints extracted
titles and statuses. Check for files it got wrong.
**Done when:** extraction works on the sample lab and your real folder without crashes.

### M3. Index and search

**Goal:** fast full-text search over files and the record.

Tasks:
1. **Database setup** (`core/index/db.py`): open SQLite with WAL mode; check FTS5 is
   available and fail with a clear message if not; create the schema from ARCHITECTURE.md;
   track `schema_version` in `meta`; if the version differs, drop and rebuild (the index is
   disposable).
   *Tests:* fresh creation; version mismatch triggers rebuild; FTS5 check.
2. **File indexing** (`core/index/files.py`): upsert files and their latest text into
   `files_fts`; record text versions.
3. **Record indexing** (`core/index/records.py`): load entries (current view) into `entries`,
   `entry_subjects`, `entries_fts`. Incremental: only new entry files since the last load,
   tracked by highest ID seen plus a periodic full reconcile.
4. **Search** (`core/index/search.py`): one `search(query, limit, project=None)` returning a mixed
   list of file hits and record hits with BM25 rank, a snippet with highlighted terms, and the
   source root label. Record hits rank slightly above file hits for equal relevance, since an
   explanation is usually what people want. Handle user input safely: quote FTS special
   characters, support simple phrases in quotes.
   *Tests:* "blocking buffer" finds the docx protocol and a note about it; a query with
   characters such as `"`, `*`, `-`, `:` never raises; Unicode query matches the Unicode folder
   file; 2,000 generated files search in under 100 ms (performance test, marked so it can be
   skipped).
5. **Rebuild** (`core/index/rebuild.py`): drop everything and rebuild from roots and record.
   *Test:* rebuild results equal incremental results for the same data (the index-is-disposable test).

**Done when:** search tests pass; rebuild equivalence holds.

### M4. Scanning, snapshots and changes

**Goal:** the app notices when a document's content changes and can show the difference.

Tasks:
1. **Snapshots** (`core/snapshots.py`): store extracted text gzipped at
   `<app-data>/snapshots/ab/<sha>.txt.gz`; `put(text) -> sha`, `get(sha) -> text`; size cap with
   eviction of oldest unreferenced snapshots; never evict snapshots referenced by record entries.
   *Tests:* dedup of identical text; eviction order; referenced snapshots survive eviction.
2. **Scanner** (`core/scan.py`): for each root with search on: list files; re-examine only on
   size/mtime change; hash; extract if hash changed; snapshot text if text hash changed; mark
   removed files `status="missing"` (don't delete their history). Return a `ScanReport`
   (new, changed, unchanged, missing, skipped with reasons, errors).
   *Tests:* first scan indexes everything; second scan with no changes re-extracts nothing;
   editing the docx protocol produces exactly one changed file and one new snapshot; touching a
   file without changing content produces no change; renaming a file is detected as a move
   (same content hash) and keeps its history.
3. **Changes and diffs** (`core/changes.py`): `pending_changes()` lists text changes that have
   no explaining entry and aren't dismissed; `diff(file, from_sha, to_sha)` returns a readable
   line diff plus a short one-line summary (first changed line pair, e.g.
   `Primary antibody incubation: 1 h at RT → 2 h at RT`); `dismiss(change)`.
   *Tests:* a note with matching `change.to_text_sha256` clears the pending change; dismiss
   persists across restarts; summaries for docx table changes are readable.
4. **Service layer** (`core/service.py`): an `Eridi` class exposing everything the
   interfaces need: `add_root`, `list_roots`, `scan`, `search`, `pending_changes`, `diff`,
   `add_note`, `add_outcome`, `file_history`, `recent`, `rebuild_index`, `doctor`. CLI, API and MCP
   call only this.
   *Tests:* an end-to-end test using only `Eridi`: add sample_lab root → scan → edit
   protocol → scan → pending change shown → add note → pending gone → search finds the note →
   `file_history` shows both versions and the note.

**Done when:** the end-to-end service test passes on all platforms.

### M5. Command line (first usable version)

**Goal:** you can use Eridi on your real lab folders from the terminal.

Commands (all thin wrappers over `service.py`, friendly output with `rich`):
```
eridi init                         # name, email, choose record folder
eridi storage add <path> [--label "Lab OneDrive"] [--no-save]
eridi storage list
eridi scan
eridi search "<query>"
eridi changes                      # pending changes needing a "why"
eridi diff <file>
eridi why <file> "<text>" [--outcome worked|partial|failed]
eridi note "<text>" [--file <file>] [--tag t]
eridi history <file>
eridi recent [--limit 20]
eridi index rebuild
eridi doctor                       # FTS5, folders reachable, conflict copies, skipped files
```
Tasks:
1. Implement commands with clear help text and error messages.
2. `eridi why` with no text opens an interactive prompt showing the diff first.
3. File arguments accept a path, a partial name (with a pick list if several match), or a file ID.

*Tests:* Typer `CliRunner` tests for every command against sample_lab, including error cases
(no init yet, unknown file, ambiguous name).

**Human check (important):** use the CLI on your real lab folders for a week. Each time a
protocol changes, run `eridi why`. Keep a short list of friction points.
**Done when:** you've used it for real for a week and logged friction.

### M6. MCP server (AI assistants can use the record)

**Goal:** ask Claude (Desktop or Code) or any MCP client "why did we change the blocking
protocol?" and get an answer grounded in your record.

Tasks:
1. `mcp/server.py` with the official `mcp` SDK, stdio transport, tools listed in
   ARCHITECTURE.md, each calling `service.py`. Results include file paths, dates, authors and
   entry IDs so answers can cite sources.
2. Read-only by default; `add_note` only if `mcp.allow_writes = true`, writing
   `author.kind = "agent"`.
3. `eridi mcp` command; `eridi mcp config` prints a ready-to-paste config snippet for
   Claude Desktop and Claude Code.
4. Docs: `docs/MCP.md` with setup for Claude Desktop, Claude Code and a generic MCP client.

*Tests:* in-process MCP client test calling each tool; write tool refused when disabled;
agent-written entries have `author.kind = "agent"`; tool output never exceeds a size cap
(long documents are truncated with a note).

**Human check:** connect Claude Desktop to your own record and ask five real "why" questions.
**Done when:** answers cite the right entries.

### M7. Local web interface (v0 complete)

**Goal:** a simple app window in the browser: search, changes waiting for a "why", history.

Tasks:
1. `api/app.py`: FastAPI on `127.0.0.1`, random port, token auth, Host and Origin checks
   (ARCHITECTURE.md, "Local API security"). `eridi open` starts the server if needed and
   opens the browser with the one-time token URL.
2. `api/routes.py`: JSON endpoints for search, changes, diff, add note/outcome, history,
   storage list and toggles, scan now.
3. `ui/`: three screens matching the vision mockup:
   - **Search**: one box, mixed results (files and notes), source labels.
   - **Changes**: each pending change with its diff summary, a "why" box, an outcome choice,
     Save / Dismiss.
   - **File history**: versions, notes and outcomes on a timeline.
   Also a **Storage** settings page with Search/Save toggles. Light and dark themes, keyboard
   accessible, works at narrow widths.
4. Background scan every 10 minutes while the server runs (configurable), plus a "Scan now" button.

*Tests:* API tests with FastAPI `TestClient`: request without token → 401; wrong Host → 400;
cross-origin POST → 403; full flow (scan → change → note → history). One Playwright smoke
test that loads the UI, searches, and saves a note (runs on Linux CI only).

**GATE A — Human check:** you and one labmate use v0 for two weeks.
Pass: you both reach for it instead of digging through drives. If not, fix v0 before Phase B.

---

## Phase B — v0.5: projects, teammates, Microsoft

### M8. Projects

**Goal:** organize by project, not by storage.

Tasks:
1. `project` entries in the record (create, rename, set home storage); `project_roots` mapping
   folders (root + path prefix) to projects.
2. CLI `eridi project new|list|add-folder|show`; UI project sidebar and project page
   (files from all storage, pending changes, record timeline).
3. Notes and searches can be scoped to a project; files are auto-assigned to a project by folder.

*Tests:* a project spanning two roots shows files from both; renaming a project via a new
entry updates views; search scoped to a project excludes others.

### M9. Shared record for a lab, and live watching

**Goal:** a lab shares one record through its existing shared folder; changes appear quickly.

Tasks:
1. Shared record folder: `eridi init --join <folder>` joins an existing record folder
   (e.g., a SharePoint-synced lab folder). Entries from all members appear.
2. File watcher (`watchdog`) on local roots and the record folder, debounced (5 s); falls back
   to periodic scans if watching fails.
3. Root markers (`.eridi-root.json`) offered with permission, so the same synced folder at
   different paths on different computers is recognized as one root.
4. `eridi doctor` reports conflict copies created by sync tools and entries from newer versions.

*Tests:* two simulated members writing to one record folder; watcher picks up a new entry file
within the debounce window; the same root at two different paths resolves to one root ID.

### M10. Microsoft OneDrive and SharePoint connector

**Goal:** connect OneDrive/SharePoint directly, including online-only files and version history.

Tasks:
1. Register a multi-tenant app in Microsoft Entra (public client, no secret). Auth with `msal`
   using the authorization code flow with PKCE and a loopback redirect; device-code flow as a
   fallback. Scopes: `Files.Read.All`, `Sites.Read.All` (read-only), `offline_access`, `User.Read`.
   Tokens stored with `keyring`.
2. `connectors/msgraph.py`: list drives (OneDrive and followed SharePoint libraries), delta
   queries for changes (`/drive/root/delta`), file download for extraction, version history
   (`/items/{id}/versions`) mapped to `RemoteVersion`.
3. Rate limiting and retries (honor `Retry-After`), paging, resumable delta cursor stored in the index.
4. UI: "Add storage → Microsoft" flow; clear message when university IT approval is required
   ("Your university needs to approve this app. Send IT this link: …").
5. `docs/IT_APPROVAL.md`: a one-page explanation for university IT: what the app accesses,
   read-only scopes, no servers, where data lives, open-source code link.

*Tests:* against recorded Graph responses (no live calls in CI): paging, delta with
deletions and moves, throttling with `Retry-After`, version mapping. Live test behind an env
flag for manual runs.

**Human check:** connect your university OneDrive. If blocked, take `IT_APPROVAL.md` to IT.

---

## Phase C — v0.6 → v1: saving, Google, outcomes, installers

### M11. Save through the app (outbox)

**Goal:** save a file into a project and have it land in the project's home storage reliably.

Tasks:
1. Outbox in `<app-data>/outbox/`: save locally first, queue an upload, retry with backoff,
   confirm. State shown in the UI (saved locally → queued → uploaded).
2. `Connector.save()` for local and Microsoft, only for roots with Save on; never overwrite an
   existing file without the user confirming (save as a new version or new name).
3. Data-safety label on projects ("contains patient data"): saving is allowed only to roots
   marked approved for that label.

*Tests:* offline save then reconnect uploads; crash mid-upload resumes; save to a root with
Save off is refused; labeled project refuses an unapproved root.

### M12. Google Drive connector

Tasks:
1. Google OAuth "desktop app" client with loopback redirect (`google-auth-oauthlib`), scope
   `drive.readonly`. Note: this is a restricted scope. While the Google Cloud project stays in
   "testing" mode, up to 100 named test users can use it without verification, which is enough
   for pilots. Public release needs Google's verification and security assessment; plan it
   for when there's funding.
2. `connectors/gdrive.py`: list files, Changes API for deltas, export native Google Docs,
   Sheets and Slides to text (Docs export as `text/plain`, Sheets as CSV), revisions list for
   history. Resolve `.gdoc` stubs from M2 to their real documents.
3. Same retry, paging and cursor handling as M10.

*Tests:* recorded responses; export of native docs; revisions mapping; stub resolution.

### M13. Outcomes and views (v1 feature)

**Goal:** the "did it work?" layer.

Tasks:
1. Outcome entries targeting a file version or an earlier entry; quick outcome buttons in
   history and changes views; `eridi outcome <file|entry> worked|partial|failed`.
2. Views: "What changed in this project this month", "Everything that failed with this
   protocol", "Open questions" (changes with no outcome after N days, configurable).
3. Export a project's record as Markdown or CSV (for lab meetings and reports).

*Tests:* outcome views; exports match fixtures.

### M14. Installers, signing and updates

**Goal:** a labmate who never opens a terminal can install it on Mac or Windows.

Tasks:
1. Build the engine and local server as a single binary per OS with PyInstaller (in CI).
2. Tauri v2 shell: native window showing the local UI, tray/menu-bar icon, starts the engine as
   a sidecar, background scanning while open.
3. Signing: macOS notarization (Apple Developer Program); Windows signing (Azure Trusted Signing).
   Secrets stored as GitHub Actions secrets.
4. Auto-update via Tauri's updater pointing at GitHub Releases (signed update manifests).
5. Release workflow: tag `v0.x.y` → build, sign, publish to Releases with checksums.

*Tests:* CI builds all installers on tags; a smoke test launches the packaged engine binary and
calls its health endpoint on each OS.

**GATE C — Human check (pilot):** 2–3 labs, including one clinical research team, use it for
four weeks. Pass: at least one PI says they'd pay or ask their department to.

---

## Phase D — v2 → v4: ask, assistants everywhere, owned model

### M15. Ask in plain English (v2)

Tasks:
1. Answer pipeline, cheapest first: structured query (dates, people, kinds) → full-text search →
   send only the top relevant entries and snippets to a model, with instructions to cite entry IDs.
2. Bring-your-own model: API key (Anthropic, OpenAI, Azure) stored in keychain, or a local
   model through Ollama. Default off. A clear notice of what text is sent.
3. Every answer shows its sources; clicking opens the entry or file.
4. Thumbs up/down and corrections are saved locally as evaluation data (see M17).

*Tests:* with a fake model: only selected snippets are sent (assert the payload); answers
without valid citations are flagged; disabled by default.

### M16. Assistant access, hardened (v3)

Tasks:
1. MCP over HTTP (streamable transport) in addition to stdio, behind the same local token.
2. Per-project permissions for MCP clients (which projects an assistant may read).
3. Publish the MCP server to public MCP registries; examples for Claude, Cursor and others.

### M17. Toward the lab's own model (v4)

Tasks:
1. Export a dataset: (question, retrieved context, answer, human correction) and
   (change diff, explanation, outcome) pairs, with personal names optionally removed.
2. Evaluation harness: run a held-out set of the lab's own questions against any model; report
   accuracy and cost.
3. Fine-tuning recipe (LoRA on a small open-weight model) documented and scripted; run only
   when a lab has a few thousand examples.

---

## What not to build yet

Accounts and servers, a hosted web version, mobile apps, OCR, email and chat connectors,
Dropbox/Box, billing. Each waits until a pilot shows it's needed.
