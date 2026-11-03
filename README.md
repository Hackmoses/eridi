# Eridi

*Status: pre-alpha.*

Every lab knows what it did. Almost none know why.

**The name.** *Eridi* (eh-REE-dee) is built from two Yoruba words: *ẹ̀rí*, evidence or
testimony, and *ìdí*, reason or cause. Together: the evidence behind the why.

Eridi is an open-source, local-first research memory for life science and clinical
research. It searches the protocols, data and documents your lab already keeps on your
computer, OneDrive and Google Drive; notices when they change; and records **why** they changed
and **whether it worked**. Your files never leave your storage, and the record stays in
your own cloud, in an open format.

AI assistants can use the record through an MCP server, so the lab's knowledge gets more
useful as AI improves.

## Status

v0, pre-alpha: the command line, the local web app and the MCP server work on local and
synced folders. Try it:

```bash
uv sync
uv run eridi init                       # your name, and where to keep the record
uv run eridi storage add "<lab folder>"
uv run eridi scan
uv run eridi search "blocking buffer"
uv run eridi changes                    # files that changed and need a why
uv run eridi why <file>                 # shows the diff, asks why
uv run eridi open                       # the web app in your browser
```

`uv run eridi --help` lists every command. To use Eridi from Claude, see
[`docs/MCP.md`](docs/MCP.md).

See:

- [`docs/PLAN.md`](docs/PLAN.md): milestones, tasks and tests
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): design and structure
- [`docs/RECORD_FORMAT.md`](docs/RECORD_FORMAT.md): the open record format
- [`docs/PROGRESS.md`](docs/PROGRESS.md): what's done
- [`docs/MCP.md`](docs/MCP.md): using Eridi from Claude and other AI assistants

## Building with an AI agent

This repo is set up for Claude Code and other coding agents. `AGENTS.md` holds the rules;
`CLAUDE.md` imports it for Claude Code. To start a session:

> Read AGENTS.md and docs/PLAN.md. Then do the next unchecked milestone in docs/PROGRESS.md,
> task by task, with tests. Stop at the milestone's Human check and tell me how to run it.

## License

Apache-2.0 (from milestone M0).
