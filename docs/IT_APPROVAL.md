# Eridi: information for university IT

A researcher has asked to connect **Eridi** to their Microsoft 365 account. This page
explains what it accesses so you can approve it.

## What Eridi is

An open-source desktop tool for research labs. It indexes the lab's own documents
(protocols, notes, data files) so people can search them, notices when a document changes,
and keeps a record of **why** it changed. It runs entirely on the researcher's computer.

Source code (Apache-2.0): <https://github.com/Hackmoses/eridi>. The Microsoft code is
in `src/eridi/connectors/msauth.py` (sign-in) and `src/eridi/connectors/msgraph.py` (reading
files).

## What it asks for

Delegated permissions only, so it acts as the signed-in user and sees only what they can
already see. **Read-only by default.** Two write permissions exist, asked for only if a
researcher turns on "Save" for a library (see below).

| Permission | Why |
|---|---|
| `Files.Read.All` | Read files in the user's OneDrive and libraries they can open, to search them and see what changed. |
| `Sites.Read.All` | List the SharePoint sites the user follows, so they can pick a lab library. |
| `User.Read` | Show who is signed in. |
| `offline_access` | Stay signed in between sessions without asking for the password each time. |

Optional, only when a researcher turns Save on for a library, to save a file they chose
into it (Eridi never replaces an existing file without asking, and never deletes or moves
files):

| Permission | Why |
|---|---|
| `Files.ReadWrite.All` | Upload the file the researcher saves through Eridi. |
| `Sites.ReadWrite.All` | The same, for SharePoint document libraries. |

To allow reading but not saving, grant admin consent for the read permissions only.

There are no application (app-only) permissions, and no access to mail, calendar, Teams
chats or other users' data. By Microsoft's defaults none of these needs an administrator;
your organization's user-consent settings decide whether researchers can approve them
themselves, which is usually why this request reached you.

## How it works

- **Public client, no secret.** Eridi is a multi-tenant app registration of type "public
  client" (mobile and desktop). Sign-in uses the OAuth 2.0 authorization code flow with PKCE
  and a `http://localhost` redirect, or the device-code flow.
- **No servers.** Eridi has no backend. The researcher's computer talks directly to
  Microsoft Graph (`graph.microsoft.com`). Nothing is sent to the Eridi project or anyone
  else. No telemetry or analytics.
- **Where data lives.**
  - Sign-in tokens: the computer's OS keychain (macOS Keychain, Windows Credential Manager).
  - Search index and extracted text: the user's local app-data folder.
  - The "why" record (short notes written by lab members): a folder the lab chooses,
    normally in its own OneDrive or SharePoint, as plain JSON files.
  - Original files are never copied elsewhere, modified, moved or deleted.
- **AI is off by default.** Optional AI features (later versions) only run if the user
  configures their own provider, and only send the snippets needed to answer a question.

## Approving it

The link the researcher sent you is Microsoft's standard admin consent page:

```
https://login.microsoftonline.com/<your tenant>/adminconsent?client_id=<Eridi's app ID>
```

Signing in there as an administrator shows the app's publisher and the permissions above,
and grants consent for your organization. You can limit it to specific users or groups
in Entra ID → Enterprise applications → Eridi → Properties ("Assignment required") and
Users and groups, and revoke it there at any time.

## Contact

Security issues: see [`SECURITY.md`](../SECURITY.md) in the repository.
Questions: open an issue at <https://github.com/Hackmoses/eridi/issues>.
