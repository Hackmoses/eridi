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

## Updates

The app checks GitHub Releases for `latest.json` at start and from the tray (Check for
updates…). If there's a newer version it asks, then downloads, verifies the update's
signature against the public key built into the app, installs and restarts (stopping the
engine first, so the installer can replace it). Builds without a key never check.

Set it up once (needs someone who will keep the key safe):

```bash
cd desktop
npx tauri signer generate -w ~/.tauri/eridi-updater.key   # asks for a password
```

- Secret `TAURI_SIGNING_PRIVATE_KEY`: the contents of `~/.tauri/eridi-updater.key`.
- Secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: its password.
- Variable `TAURI_UPDATER_PUBKEY`: the contents of `~/.tauri/eridi-updater.key.pub`.

Keep the private key and password backed up somewhere safe. **The public key can never
change**: installed copies only accept updates signed with its private key. If it is lost,
everyone has to download the next version by hand.

The Release workflow then builds signed update bundles (`.app.tar.gz` on macOS, the NSIS
installer on Windows, the AppImage on Linux), collects them with
`scripts/collect_bundles.py`, and writes `latest.json` with `scripts/update_manifest.py`.

## Releasing

1. Set the new version in `pyproject.toml`, `desktop/package.json`,
   `desktop/src-tauri/Cargo.toml` and `desktop/src-tauri/tauri.conf.json` (then `uv lock`
   and, in `desktop/`, `npm install` so the lock files agree). Commit.
2. Tag and push: `git tag v0.6.0 && git push origin v0.6.0`.
3. The Release workflow checks the tag matches the version and runs the tests; builds and
   smoke-tests the engine on each OS; builds, signs and notarizes the installers; then
   publishes a GitHub release with the installers, the standalone engines, `latest.json`
   (when the updater key is set) and `SHA256SUMS`. A tag with a hyphen (`v0.6.0-rc.1`) is
   published as a pre-release, which the updater ignores.

To try a build without publishing: Actions → Release → Run workflow.
