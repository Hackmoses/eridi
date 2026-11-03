# Desktop app (installers)

The desktop app is how a labmate who never opens a terminal uses Eridi. It has two parts:

| Part | What it is | Where |
|---|---|---|
| Engine | The Python app (CLI, local web app, MCP server) packaged as one executable with PyInstaller | `packaging/`, `scripts/build_engine.py` |
| Shell | A small Tauri v2 app: a native window showing the local web app, and a tray / menu-bar icon | `desktop/` |

The shell starts the engine as a *sidecar* with `eridi-engine serve --sidecar`. They talk in
JSON lines: the engine prints `{"event": "ready", "port", "url", "version"}` (the URL is a
one-time sign-in link to `http://127.0.0.1:<port>`), the shell shows that page, and writes
`link`, `scan` or `quit` to the engine's stdin. When the shell exits, or crashes, the
engine's stdin closes and it stops. The engine's pages get no access to the shell (see
`desktop/src-tauri/capabilities/default.json`), and the shell only ever navigates to
`http://127.0.0.1`.

Closing the window keeps Eridi in the tray, so watching and background scans go on. The
tray menu has Open Eridi, Scan now, Check for updates… and Quit Eridi.

## Build it yourself

Needs Python with `uv`, Rust (stable), and Node.js 18+. On Linux, Tauri also needs
WebKitGTK and friends (`libwebkit2gtk-4.1-dev libxdo-dev libssl-dev
libayatana-appindicator3-dev librsvg2-dev`).

```bash
uv sync
uv run python scripts/build_engine.py        # → desktop/src-tauri/binaries/eridi-engine-<triple>
uv run python scripts/smoke_engine.py desktop/src-tauri/binaries/eridi-engine-<triple>
cd desktop && npm ci
npm run tauri dev                            # run the app
npm run tauri build                          # installers in desktop/src-tauri/target/release/bundle/
```

Local builds are unsigned and never check for updates (no updater key is built in).

## Version

One version everywhere: `pyproject.toml`, `desktop/package.json`,
`desktop/src-tauri/Cargo.toml` and `desktop/src-tauri/tauri.conf.json`. A test checks they
agree.
