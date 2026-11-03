# Signing, notarization and update keys

Unsigned installers work, but macOS refuses to open them without a right-click → Open,
and Windows SmartScreen warns. Releases are signed in the Release workflow once these
secrets exist (repository **Settings → Secrets and variables → Actions**). Each part is
skipped when its secrets are missing. None of this can be done by an agent: it needs the
project's own accounts.

## macOS: Developer ID and notarization (Apple Developer Program, about $99/year)

1. Join the Apple Developer Program as an organization or individual.
2. In Xcode or developer.apple.com, create a **Developer ID Application** certificate.
   Export it from Keychain Access as a `.p12` with a password.
3. Create an **app-specific password** for your Apple ID at appleid.apple.com.
4. Add these **secrets**:

| Secret | Value |
|---|---|
| `APPLE_CERTIFICATE` | `base64 -i certificate.p12` (one line) |
| `APPLE_CERTIFICATE_PASSWORD` | the `.p12` password |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID1234)` |
| `APPLE_ID` | the Apple ID email |
| `APPLE_PASSWORD` | the app-specific password |
| `APPLE_TEAM_ID` | the 10-character team ID |

What happens: the engine job signs every library inside the engine with PyInstaller
(hardened runtime, `packaging/entitlements.plist`; Python needs to load the libraries it
unpacks). Tauri then signs the app and the engine inside it and sends the `.dmg` to Apple
for notarization.

## Windows: Azure Trusted Signing (about $10/month)

1. In Azure, create a **Trusted Signing account** and a **certificate profile** (public
   trust), after Microsoft's identity validation for the project.
2. Create an app registration with a client secret, and give it the **Trusted Signing
   Certificate Profile Signer** role on the account.
3. Add **secrets** `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`, and
   **variables** `AZURE_SIGNING_ENDPOINT` (e.g. `https://eus.codesigning.azure.net`),
   `AZURE_SIGNING_ACCOUNT`, `AZURE_SIGNING_PROFILE`.

What happens: the engine `.exe` is signed in the engine job, and Tauri signs the app and
both installers with `trusted-signing-cli` (`scripts/release_config.py` adds the command).

## Updates: the updater key (free)

See `docs/DESKTOP.md`, "Updates".

## Check a signed build

- macOS: `codesign --verify --deep --strict --verbose=2 /Applications/Eridi.app` and
  `spctl --assess --type exec -vv /Applications/Eridi.app` (says "Notarized Developer ID").
- Windows: right-click the installer → Properties → Digital Signatures.
