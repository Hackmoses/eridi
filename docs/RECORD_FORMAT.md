# Record format — schema `eridi.record/1`

The record is the product's permanent asset. It must outlive the app, be readable by other
tools, sync safely through OneDrive / Google Drive / SharePoint, and merge cleanly when several
people write to it. This document is a contract. Changes follow the versioning rules at the end.

## Location and layout

The user chooses a **record folder**, normally inside a synced cloud folder (for example
`OneDrive - University/Eridi/` or a lab's shared SharePoint folder). Until they choose one,
the app uses `<app-data>/record/`.

```
<record folder>/
  eridi-record.json          # folder marker: {"schema": "eridi.recordset/1", "id": "<ulid>", "created_at": "..."}
  entries/
    2026/
      11/
        01JC8Z9K3V5W6X7Y8Z9A0B1C2D.json
        01JC8ZA1...json
  snapshots/                      # optional, see "Snapshots"
```

- **One entry per file.** File name is the entry's ULID plus `.json`. ULIDs sort by time and
  are unique across devices, so two people writing at once never collide. This is what makes the
  record safe inside a sync folder (a single database file there would corrupt).
- Subfolders by UTC year and month keep folder sizes manageable.
- Files are UTF-8 JSON, pretty-printed with 2-space indent, keys sorted, trailing newline.
- **Write atomically:** write to `entries/.tmp/<ulid>.json.tmp`, fsync, then rename into place.
  Readers ignore `.tmp/` and any file not matching `^[0-9A-HJKMNP-TV-Z]{26}\.json$`.
- Sync tools sometimes create conflict copies such as `<ulid> (1).json` or
  `<ulid>-DESKTOP-ABC.json`. Readers ignore names that don't match the pattern and report them
  in `eridi doctor`.

## Entry fields

```json
{
  "schema": "eridi.record/1",
  "id": "01JC8Z9K3V5W6X7Y8Z9A0B1C2D",
  "kind": "note",
  "created_at": "2026-11-03T14:05:22.481Z",
  "author": { "name": "A. Okafor", "email": "a.okafor@example.edu", "kind": "human" },
  "device_id": "dev_01JC8Y...",
  "project_id": "proj_01JC8X...",
  "subjects": [
    {
      "root_id": "root_01JC8W...",
      "root_label": "OneDrive – University",
      "path": "Protocols/Western_blot_blocking_v7.docx",
      "content_sha256": "9f2c…",
      "text_sha256": "41ab…"
    }
  ],
  "change": {
    "from_text_sha256": "77de…",
    "to_text_sha256": "41ab…",
    "summary": "Primary antibody incubation: 1 h at RT → 2 h at RT"
  },
  "text": "Signal was weak with the new antibody lot (#B2291). Longer incubation restored band intensity.",
  "outcome": "worked",
  "tags": ["antibody", "lot-change"],
  "supersedes": null,
  "extra": {}
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Always `eridi.record/1` for this version. |
| `id` | yes | ULID, uppercase Crockford base32. Equals the file name stem. |
| `kind` | yes | `note` (a why/explanation), `decision` (a choice made, with or without a file), `outcome` (a result for an earlier entry or a file version), `link` (connects entries or files), `project` (creates or renames a project), `tombstone` (retracts an entry). |
| `created_at` | yes | UTC, ISO 8601 with milliseconds and `Z`. |
| `author.name` | yes | Display name from the user's settings. |
| `author.email` | no | If known. |
| `author.kind` | yes | `human` or `agent`. Entries written through MCP by an AI assistant are `agent`. |
| `device_id` | yes | Random per-install ID, for debugging merges. Not personally identifying. |
| `project_id` | no | The project this entry belongs to, or `null`. |
| `subjects` | yes (may be empty) | Files this entry is about. `path` is POSIX-style, relative to the root. Hashes let the app re-find a file that was moved or renamed. |
| `change` | no | Present when the entry explains a detected change between two text versions. |
| `text` | yes (may be empty for `outcome`/`link`/`tombstone`) | The human words. Plain text; Markdown allowed. |
| `outcome` | no | `worked`, `partial`, `failed`, `unknown`, or `null`. |
| `tags` | no | Lowercase strings. |
| `supersedes` | no | ID of an earlier entry this one corrects. The newer entry wins in views; history keeps both. |
| `extra` | no | Object for forward-compatible data. Readers preserve but don't interpret unknown keys. |

Kind-specific fields:
- `outcome` entries set `target_id` (the entry they evaluate) or a `subjects` file version.
- `link` entries set `links: [{"from": "<id or subject>", "to": "<id or subject>", "relation": "caused|replaces|relates"}]`.
- `project` entries set `project: {"id": "proj_…", "name": "Antibody validation", "home_root_id": "root_…"}`. The latest `project` entry for an ID defines its current name and home.
  Optional (added in M8, within `/1`): `project.folders: [{"root_id": "root_…", "path": "Protocols/WB"}]`,
  the project's complete folder list as of that entry. `path` is POSIX-style, relative to the
  root; `""` means the whole root. Files in these folders belong to the project, and so do
  entries about them. A `project` entry without `folders` keeps the folders of the project's
  previous entry (so entries written before M8 never clear them). Fixture:
  `tests/fixtures/record/v1/project_folders.json`.
- `tombstone` entries set `target_id`. Views hide the target; history shows that it was retracted and by whom.

## Roots

A **root** is a connected storage location (a local folder, a OneDrive drive, a Google Drive).
Each root has a stable `root_id` stored in a small marker so that the same synced folder is
recognized on different computers, even at different paths:

- For local and synced folders: `<root>/.eridi-root.json` containing `{"root_id": "...", "label": "..."}`.
  The app creates this only with the user's permission, since it writes into their folder. If
  not permitted, the root ID is stored only in local config and won't match across machines.
- For cloud connectors: derived from the provider's drive ID (`ms:<driveId>`, `gd:<driveId>`).

## Snapshots

To show what changed in files that keep no history (plain local files), the app stores the
**extracted text** of each version it has seen, never the original binary file:

- Location: `<app-data>/snapshots/<first 2 hex>/<text_sha256>.txt.gz` (local only, by default).
- Optional setting "Share snapshots with the lab" also writes them to
  `<record folder>/snapshots/` so teammates see the same diffs.
- Content-addressed, so identical text is stored once. Size cap is configurable (default 500 MB),
  oldest unreferenced snapshots evicted first. Snapshots referenced by a record entry's `change`
  are never evicted.

## Reading rules

- Read every valid entry file. Sort by `id`.
- Unknown `kind` → keep it, show it as "Unsupported entry" in views, never drop it.
- Unknown fields → preserve in memory, ignore in logic.
- A schema with an unknown **major** version (`eridi.record/2` when the reader only knows 1)
  → skip it, and warn once: "Some record entries were written by a newer version. Update to see them."
- Invalid JSON or missing required fields → skip, and list it in `eridi doctor`.

## Versioning rules

- Additive, optional fields: allowed within `/1`. Document them here.
- Anything that changes meaning, removes or renames a field, or makes an optional field
  required: new major version (`/2`), with a reader for `/1` kept forever and fixture tests for
  both.
- Fixtures for each version live in `tests/fixtures/record/v1/`.
