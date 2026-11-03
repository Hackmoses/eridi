# Contributing to Eridi

Thanks for helping. Eridi is built in small milestones; see `docs/PLAN.md` for what's next and
`docs/PROGRESS.md` for what's done.

## Set up

```bash
uv sync
uv run pre-commit install
```

## Before opening a pull request

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

All four must pass. CI runs them on macOS, Windows and Linux.

## Rules

Read `AGENTS.md`. It applies to people as much as to AI agents, especially the rules that
must never be broken: connectors are read-only, the record is append-only, and no document
content leaves the user's machine.

## Sign your commits (DCO)

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/).
Add a sign-off line to each commit:

```bash
git commit -s -m "M2.3: extract text from pptx"
```

## Using an AI agent

Agents are welcome. Point yours at `AGENTS.md`; Claude Code reads `CLAUDE.md`, which imports it.
You're responsible for what your agent submits, and the same checks apply.
