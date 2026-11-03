# AGENTS.md — instructions for any coding agent

This file is the entry point for Claude Code, Cursor, Codex, Copilot agents, or any other
AI agent working in this repository. Humans should read it too.

## What this project is

**Eridi** is an open-source, local-first research memory for life science
and clinical research labs. It indexes the files a lab already has (local folders, OneDrive,
Google Drive), lets people search them, detects when protocols and documents change, and
records **why** things changed and **whether they worked**. That record becomes the lab's own
intelligence, queryable by people and by AI assistants.

Read these before writing code:

1. `docs/PLAN.md` — the milestones, in order, with tasks and tests. **This is your work queue.**
2. `docs/ARCHITECTURE.md` — the structure, module boundaries and key decisions.
3. `docs/RECORD_FORMAT.md` — the on-disk format of the record. Treat it as a contract.
4. `docs/PROGRESS.md` — what is done. Update it as you finish tasks.
5. `docs/LANDSCAPE.md` — nearby products and how Eridi stays different.

## How to work

1. **One milestone at a time.** Find the first unchecked milestone in `docs/PROGRESS.md`.
   Do its tasks in order. Do not start the next milestone.
2. **Tests with every task.** Each task in `docs/PLAN.md` lists the tests it needs. Write them
   alongside the code. A task is done only when its tests pass.
3. **Run the full check before saying a task is done:**
   ```bash
   uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
   ```
4. **Stop at every "Human check".** Each milestone ends with a manual check a person must do.
   Summarize what you built, how to run the check, and wait.
5. **Small commits**, one per task where practical, with clear messages
   (`M3.2: add FTS5 search over file text`).
6. **Update `docs/PROGRESS.md`** after each task: tick the box and add a one-line note if
   anything differs from the plan.
7. **If the plan is wrong, say so.** Propose the change in your summary and, once agreed,
   update `docs/PLAN.md`. Don't silently deviate.

## Rules that must never be broken

- **Never modify, move or delete a user's source files.** Connectors are read-only. The only
  places the app writes are its own app-data directory and the user-chosen record folder
  (and, from milestone M11, files the user explicitly saves through the app).
- **The record is append-only.** Never rewrite or delete a record entry file. Corrections are
  new entries that `supersede` old ones. Deletion is a `tombstone` entry. See
  `docs/RECORD_FORMAT.md`.
- **Changing the record format requires** bumping the schema version, a reader that still
  accepts all older versions, and tests with fixtures of the old format.
- **The index is disposable.** Anything stored only in the SQLite index must be rebuildable
  from the source files plus the record. If it can't be rebuilt, it belongs in the record.
- **No document content leaves the machine** except to destinations the user configured
  (their own cloud storage, or an AI provider they explicitly enabled). No telemetry, no
  analytics, no crash reporting that includes file text. Logs must never contain document text.
- **The local web server binds to `127.0.0.1` only** and requires the per-install access token
  (see ARCHITECTURE.md, "Local API security").
- **Secrets (OAuth tokens, API keys) go in the OS keychain** via `keyring`, never in plain files
  or logs.
- **Cross-platform from day one.** macOS and Windows are first-class; Linux runs in CI. Use
  `pathlib`, never hard-code separators, and store paths in the record as POSIX-style paths
  relative to a root.
- **No new runtime dependency** without noting why in the commit message. Prefer the standard
  library.

## Commands

```bash
uv sync                      # install dependencies
uv run pytest                # tests
uv run pytest -k record      # a subset
uv run ruff check . --fix    # lint
uv run ruff format .         # format
uv run mypy src              # type check
uv run eridi --help          # the CLI (from milestone M5)
```

## Repository layout

```
src/eridi/
  core/         engine: models, config, record store, index, scanner, extractors, diffs
  connectors/   one module per storage source (local first)
  cli/          command line (Typer)
  api/          local HTTP API (FastAPI), serves the UI
  ui/           static web UI (HTML/CSS/JS, no build step)
  mcp/          MCP server so AI assistants can use the record
tests/          pytest; fixtures build a sample lab folder at test time
docs/           plan, architecture, record format, progress
```

## Guiding principles

- **Make the best decision that will elevate all three: the user experience (UX), developer
  experience (DX), and agent experience (AX) always, without breaking anything.**
- **Cut taglines, helper text and secondary labels. If a screen needs them to make sense, fix
  the layout.**

## Style

- Python 3.12+, type hints everywhere, `mypy --strict` on `src/`.
- Small modules with clear names. Pure functions in `core/` where possible; I/O at the edges.
- Errors shown to users say what went wrong and how to fix it.
- User-facing words: "project", "storage", "record", "note", "outcome". Avoid internal jargon
  in the UI and CLI output.
