# Google Drive

Eridi reads Google Drive directly through the Drive API: files in My Drive and shared
drives, native Google Docs, Sheets and Slides (exported to text), and the revision history
Google keeps. Access is read-only (`drive.readonly`).

## Connect

```bash
eridi storage add-google            # signs in in your browser, then lists your drives
eridi storage add-google --all      # add My Drive and every shared drive
```

Then `eridi scan`. Native Google documents appear as `Name.gdoc`, `Name.gsheet` and
`Name.gslides`, the same names Google's Drive for desktop app gives them, so a note about a
document lines up whether you reach it through the sync folder or directly. If a synced
folder holds `.gdoc` shortcuts, Eridi resolves them to the real document in a connected
Google Drive.

## Testing mode (pilots) and verification (public release)

`drive.readonly` is a **restricted** scope. Until Google verifies Eridi:

- the Google Cloud project stays in **testing** mode;
- only **test users** listed in the project (up to 100) can sign in; anyone else sees
  "access blocked";
- test users' sign-ins expire after 7 days, so they sign in again weekly
  (`eridi storage add-google`).

That's enough for pilot labs. A public release needs Google's OAuth verification and a
third-party security assessment for restricted scopes; plan it when there's funding.

## Set up the Google app (once, for the project)

Not something an agent can do: it needs a Google account that owns the project.

1. In <https://console.cloud.google.com/>, create a project, e.g. "Eridi".
2. **APIs & Services → Library**: enable the **Google Drive API**.
3. **OAuth consent screen**: user type **External**, publishing status **Testing**. Add the
   scope `.../auth/drive.readonly`. Under **Test users**, add each pilot user's Google
   account.
4. **Credentials → Create credentials → OAuth client ID**, type **Desktop app**.
5. Put the client ID and secret in `DEFAULT_CLIENT_ID` / `DEFAULT_CLIENT_SECRET` in
   `src/eridi/connectors/gauth.py` for releases, or for your own copy set
   `settings.google_client_id` and `settings.google_client_secret` in `config.toml` (or the
   `ERIDI_GOOGLE_CLIENT_ID` / `ERIDI_GOOGLE_CLIENT_SECRET` environment variables).

Google treats a desktop app's client secret as not confidential: it ships inside every copy.
What protects users is their own sign-in tokens, which Eridi keeps in the OS keychain.

## Where things live

| What | Where |
|---|---|
| Sign-in tokens | OS keychain, service `eridi-google` |
| Each drive's item list and change cursor | the index (disposable; rebuilt by listing again) |
| Extracted text | the index and `<app-data>/snapshots/`, as for every storage |

Nothing goes anywhere but Google and your computer.
