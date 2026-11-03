# Microsoft OneDrive and SharePoint

Eridi reads OneDrive and SharePoint document libraries directly through Microsoft Graph,
including files that are online-only on your computer and the version history Microsoft
keeps. Access is read-only. For what Eridi reads and why, written for university IT, see
[`IT_APPROVAL.md`](IT_APPROVAL.md).

## Connect

```bash
uv run eridi storage add-microsoft          # opens the Microsoft sign-in page
uv run eridi storage add-microsoft --device-code   # no browser on this computer
uv run eridi scan
```

Or in the web app: **Storage → Add Microsoft storage**.

After signing in, Eridi lists your OneDrive and the SharePoint sites you follow; pick the
libraries to search. To see a SharePoint library here, follow its site in SharePoint first.

## The app registration (maintainers)

Eridi signs in as a **multi-tenant public client** registered in Microsoft Entra. It has
no client secret, so nothing but the user's own computer can sign in with it. To register
it (once, for the project), or your own for development:

1. Go to <https://entra.microsoft.com> → **App registrations** → **New registration**.
2. Name: `Eridi`. Supported account types: **Accounts in any organizational directory and
   personal Microsoft accounts** (multi-tenant).
3. Redirect URI: platform **Public client/native (mobile & desktop)**, URI
   `http://localhost`.
4. After creating it, under **Authentication** turn on **Allow public client flows** (for
   the device-code fallback).
5. Under **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated**,
   add `Files.Read.All`, `Sites.Read.All`, `User.Read` and `offline_access`. Do not add any
   write permissions.
6. Copy the **Application (client) ID**. Put it in `DEFAULT_CLIENT_ID` in
   `src/eridi/connectors/msauth.py` for releases, or for your own copy set either
   `settings.microsoft_client_id` in `config.toml` or the `ERIDI_MS_CLIENT_ID` environment
   variable.

Publisher verification (Entra → **Branding & properties**) makes the consent screen show
a verified publisher and lowers the chance that a university blocks the app; it needs a
Microsoft Partner Network ID.

## Tokens

Sign-in tokens are stored in the operating system's keychain (macOS Keychain, Windows
Credential Manager), never in files or logs. Removing a Microsoft storage doesn't sign
you out of other Microsoft storage on the same account.

## Tests

CI uses recorded Graph responses (`tests/fixtures/msgraph/`), never the network. To run
the live test against a real account:

```bash
ERIDI_LIVE_MSGRAPH=1 ERIDI_MS_CLIENT_ID=<app id> uv run pytest tests/test_msgraph_live.py -s
```
