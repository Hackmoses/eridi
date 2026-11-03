# Using Eridi from AI assistants (MCP)

Eridi includes an [MCP](https://modelcontextprotocol.io) server, so Claude Desktop, Claude
Code and other MCP clients can search your lab's files and record, and answer "why did we
change this?" with citations. The assistant runs on your computer and talks to Eridi
directly. Eridi itself sends nothing anywhere. Your assistant's provider does see the
results it reads, as with anything else you show it.

## Before you start

Set up Eridi and scan at least once, so there is something to find:

```bash
eridi init
eridi storage add "<your lab folder>"
eridi scan
```

The MCP server reads the index as it was at the last scan. Run `eridi scan`, or keep the
Eridi app open (it scans in the background), to keep answers current.

Then print the settings for your computer:

```bash
eridi mcp config
```

It prints the exact command for your installation. Use it in the steps below in place of
`/path/to/eridi`.

## Claude Desktop

1. Open the config file. Create it if it doesn't exist:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

   In Claude Desktop you can also open it from Settings → Developer → Edit Config.
2. Add the `eridi` entry from `eridi mcp config` under `mcpServers`:

   ```json
   {
     "mcpServers": {
       "eridi": { "command": "/path/to/eridi", "args": ["mcp"] }
     }
   }
   ```

   If the file already has other servers, add `"eridi": {…}` next to them.
3. Quit Claude Desktop completely and reopen it. Eridi's tools appear in the tools menu.

## Claude Code

```bash
claude mcp add eridi -- /path/to/eridi mcp
```

Add `--scope user` to use it in every project. Check it with `claude mcp list`.

## Any other MCP client

Eridi uses the stdio transport. Configure the client to start this command:

```
/path/to/eridi mcp
```

To use a different Eridi data folder (for testing, for example), set the environment
variable `ERIDI_HOME` for that command.

## Tools

All tools are read-only unless you allow writing.

| Tool | What it does |
|---|---|
| `search(query, limit)` | Files and record entries together, with highlighted snippets. Quotes make a phrase. |
| `file_history(file)` | A file's text versions, renames, and every entry about it, including corrected and retracted ones (labeled). |
| `get_entry(entry_id)` | One record entry. |
| `list_recent(project?, kind?, limit)` | The newest current entries. |
| `pending_changes(project?)` | Changed files nobody has explained yet, with a one-line summary of each change. |
| `show_diff(file, from_version?, to_version?)` | What changed in a file, as a line diff. |
| `add_note(text, file?, outcome?, project?)` | Only when writing is allowed (below). |

`file` can be a file ID from another tool's result (`root_…:path`), a path, or part of a
file name. Results include entry IDs, file paths, storage names, authors and dates, so the
assistant can cite them. Long results are shortened, with a note saying so.

## Letting assistants add notes

Off by default. To turn it on, add this to Eridi's `config.toml` (`eridi mcp config` shows
where the file is), then restart the assistant:

```toml
[mcp]
allow_writes = true
```

Notes written this way are marked `author.kind = "agent"` and named "AI assistant (for
<your name>)", so everyone can tell them apart from notes people wrote. Like every entry,
they can be corrected or retracted, never silently changed.

## Things to ask

- "Why did we change the blocking protocol?"
- "What changed in the western blot protocol last month, and did it work?"
- "Which protocol changes still have no explanation?"
- "What have we recorded about antibody lot B2291?"

## Troubleshooting

- **No Eridi tools appear:** run the command from `eridi mcp config` in a terminal. It
  should wait silently (press Ctrl+C to stop). If it prints an error, fix that first.
  `eridi doctor` checks the rest.
- **Answers miss recent changes:** run `eridi scan`.
- **"No file matches…" or "matches several files":** ask with more of the file's path.
