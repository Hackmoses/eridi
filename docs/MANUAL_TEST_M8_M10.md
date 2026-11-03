# Testing M8–M10 by hand, with demo data

About 45 minutes. Everything runs on your own computer, against demo folders and a
separate demo Eridi setup, so your real files and any real Eridi setup are never touched.
The Microsoft part (M10) uses a free demo Microsoft account.

Commands are for **macOS** (and Linux) in Terminal. For **Windows PowerShell**, see
[Windows](#windows) at the end. Lines starting with `#` are comments; don't type them.

---

## 0. Set up (5 min)

1. Get the code and install it:

   ```bash
   git clone https://github.com/Hackmoses/eridi.git
   cd eridi
   git checkout claude/beautiful-hamilton-jymds2
   uv sync
   ```

   No `uv`? Install it first: `curl -LsSf https://astral.sh/uv/install.sh | sh`, then
   open a new Terminal window.

2. Make the demo folders. This creates `~/eridi-demo/Lab Drive` (a Word protocol, an Excel
   lot log, PDFs, notes, an R script, a notebook) and `~/eridi-demo/Shared Drive`
   (imaging settings and an admin folder):

   ```bash
   uv run python scripts/demo_lab.py create ~/eridi-demo
   ```

3. Point Eridi at a demo setup, **in every Terminal window you use for this test**.
   `ERIDI_HOME` is where Eridi keeps its settings and index; this keeps the demo apart
   from anything real. Think of it as "computer A":

   ```bash
   export ERIDI_HOME=~/eridi-demo/computer-A
   ```

4. Set up and connect both demo folders:

   ```bash
   uv run eridi init --name "A. Okafor" --email "" --record-folder ~/eridi-demo/"Lab Record"
   uv run eridi storage add ~/eridi-demo/"Lab Drive" --label "Lab Drive" --mark
   uv run eridi storage add ~/eridi-demo/"Shared Drive" --label "Shared Drive" --no-mark
   uv run eridi scan
   ```

   ✅ Expect: `Lab Drive: 9 new …` and `Shared Drive: 2 new …`.

---

## 1. M8 – Projects (10 min)

### In the terminal

1. **A project spanning two storage folders.**

   ```bash
   uv run eridi project new "Antibody validation" --folder ~/eridi-demo/"Lab Drive/Protocols" --folder ~/eridi-demo/"Shared Drive/Imaging"
   uv run eridi project new "Admin" --folder ~/eridi-demo/"Shared Drive/Admin"
   uv run eridi project list
   ```

   ✅ Expect both projects, with `Lab Drive / Protocols` and `Shared Drive / Imaging`
   under Antibody validation.

2. **Search scoped to a project excludes the others.**

   ```bash
   uv run eridi search antibody
   uv run eridi search antibody --project "antibody"
   ```

   ✅ The first lists files from everywhere, including `Inventory/Antibody_lot_log_2026.xlsx`
   and `Admin/budget_2027.md`. The second lists only the Western blot protocol and
   `Imaging/confocal_settings.md`. (A project can be named by any part of its name.)

3. **Changes in a project, and notes joining it automatically.** Simulate a labmate
   saving a new version of the protocol, then scan:

   ```bash
   uv run python scripts/demo_lab.py edit ~/eridi-demo
   uv run eridi scan
   uv run eridi changes --project "Antibody validation"
   uv run eridi changes --project Admin
   ```

   ✅ The first shows `Primary antibody incubation: 1 h at RT → 2 h at RT`; the second
   says `Nothing waiting for a why.`

   ```bash
   uv run eridi why Western_blot "New antibody lot B2291 needs longer incubation." --outcome worked
   uv run eridi note "Order more mounting medium." --project Admin
   ```

   ✅ The first ends `… (explains the latest change) in Antibody validation.`: you didn't
   name the project; the note joined it because the file is in the project's folder.
   The second ends `in Admin.`

4. **The project page in the terminal, and renaming.**

   ```bash
   uv run eridi project show antibody
   uv run eridi project rename antibody "Antibody validation 2026"
   uv run eridi project show "2026"
   ```

   ✅ `show` lists the files from both storage folders, no "Needs a why" (you explained
   it), and the record with your note. After renaming, the folders are unchanged.

### In the web app

5. Start it:

   ```bash
   uv run eridi open
   ```

   Your browser opens Eridi. Leave this Terminal window running; use a second window
   (with the same `export ERIDI_HOME=…` line) for other commands.

6. Check, and tick off:
   - [ ] The **Projects** sidebar on the left shows both projects. Click one.
   - [ ] The project page shows **Folders**, **Files** from both Lab Drive and Shared
     Drive, and **Record** with your note.
   - [ ] Type `laser` in **Search Antibody validation 2026** → only `confocal_settings.md`.
   - [ ] **Add a note** on the page → it appears at the top of Record.
   - [ ] Type a new project name in the sidebar box → **Add** → its page opens. Paste
     `~/eridi-demo/Shared Drive/Admin` (spell out your home folder, e.g.
     `/Users/you/eridi-demo/Shared Drive/Admin`) into the folder box → **Add folder** →
     the budget file appears under Files. **Remove** takes it away again.
   - [ ] **Rename** next to the title works.
   - [ ] Run `uv run python scripts/demo_lab.py edit ~/eridi-demo` in the other window.
     Within a few seconds the project page's **Needs a why** box appears after a reload,
     and the **Changes** badge in the top bar counts 1. Save a why from the project page.
   - [ ] Narrow the window to phone width: the sidebar becomes a row above the page.

   Stop the app with Ctrl+C when done.

---

## 2. M9 – A shared record, and live watching (10 min)

You'll pretend to be two lab members on two computers. Each Terminal window is one
computer (its own `ERIDI_HOME`). The record folder `~/eridi-demo/Lab Record` plays the
part of a shared OneDrive/SharePoint folder, and a copy of `Lab Drive` plays the part of
the same synced folder appearing at a different path on the second computer.

1. **Computer B joins the lab's record.** Open a **second Terminal window**:

   ```bash
   cd eridi    # the folder you cloned into
   export ERIDI_HOME=~/eridi-demo/computer-B
   cp -R ~/eridi-demo/"Lab Drive" ~/eridi-demo/"Lab Drive (computer B)"
   uv run eridi init --name "B. Lee" --email "" --join ~/eridi-demo/"Lab Record"
   ```

   ✅ Expect `… entries from A. Okafor.`

2. **The same folder at a different path is recognized as one storage.** In step 0 you
   added Lab Drive with `--mark`, which wrote a small `.eridi-root.json` into it; the copy
   carries it, just as sync would.

   ```bash
   uv run eridi storage add ~/eridi-demo/"Lab Drive (computer B)"
   uv run eridi scan
   uv run eridi history Western_blot
   ```

   ✅ `Recognized from its folder marker.`, and the history shows A. Okafor's note
   about the protocol, on computer B.

3. **Notes appear on the other computer within seconds.** In window **A**:

   ```bash
   uv run eridi watch
   ```

   In window **B**:

   ```bash
   uv run eridi note "Gel imager booked for Friday."
   ```

   ✅ Window A prints `… note  B. Lee: Gel imager booked for Friday.` within a few
   seconds, without a scan. Now edit the protocol from window B:

   ```bash
   uv run python scripts/demo_lab.py edit ~/eridi-demo
   ```

   ✅ Window A prints `edited  Lab Drive · Protocols/Western_blot_blocking_v7.docx`.
   Press Ctrl+C in window A.

   (The same happens in the web app: with `uv run eridi open` running on A, B's notes
   show up in Search and on project pages.)

4. **Asking before writing the marker.** In window B, connect a folder without saying
   `--mark` or `--no-mark`:

   ```bash
   mkdir -p ~/eridi-demo/"B notes"
   uv run eridi storage add ~/eridi-demo/"B notes"
   ```

   ✅ It asks `Shared with your other computers or your lab? Mark it …`. Answer `n`:
   nothing is written into the folder (`ls -a ~/eridi-demo/"B notes"` is empty). Then:

   ```bash
   uv run eridi storage mark "B notes"
   uv run eridi storage list
   ```

   ✅ `is marked`, and `storage list` shows `marked` for it. (The web app's Storage page
   has a **Mark for other computers** button for the same thing.)

5. **`eridi doctor` explains sync conflict copies.** Sync apps sometimes make copies
   like `… (1).json`. Make one by hand, check, then delete it:

   ```bash
   F=$(ls ~/eridi-demo/"Lab Record"/entries/*/*/*.json | head -1)
   cp "$F" "${F%.json} (1).json"
   uv run eridi doctor
   rm "${F%.json} (1).json"
   ```

   ✅ `! Sync conflict copy: … a duplicate your sync app made of entry …, which is intact.
   Eridi ignores it; you can delete it.`

---

## 3. M10 – Microsoft OneDrive and SharePoint (20 min)

### 3a. A demo Microsoft account

Use an account with nothing real in it:

- **Easiest:** a free personal Microsoft account. Create one at
  <https://signup.live.com> (an outlook.com address). It comes with OneDrive (5 GB).
  Personal accounts have no SharePoint, so you'll test OneDrive only.
- **To also test SharePoint and the IT-approval message:** a Microsoft 365 test tenant,
  e.g. from the Microsoft 365 Developer Program
  (<https://developer.microsoft.com/microsoft-365/dev-program>; eligibility varies) or
  a trial tenant. Optional; see 3f.

### 3b. Register a demo Eridi app (once, 5 min)

Eridi needs an app ID from Microsoft before it can sign in. Register one with your demo
account:

1. Go to <https://entra.microsoft.com> and sign in with the demo account. (With a
   personal account, if Entra says you have no directory, sign in at
   <https://portal.azure.com> once to create one, then come back.)
2. **App registrations** → **New registration**:
   - Name: `Eridi demo`
   - Supported account types: **Accounts in any organizational directory and personal
     Microsoft accounts**
   - Redirect URI: platform **Public client/native (mobile & desktop)**, value
     `http://localhost`
   - **Register**
3. **Authentication** → **Advanced settings** → **Allow public client flows: Yes** →
   **Save**.
4. **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated
   permissions** → tick `Files.Read.All`, `Sites.Read.All`, `User.Read`,
   `offline_access` → **Add permissions**.
5. **Overview** → copy the **Application (client) ID**. In every Terminal window you use
   from now on (window A is enough):

   ```bash
   export ERIDI_MS_CLIENT_ID=<paste the ID>
   ```

### 3c. Put demo files in OneDrive

1. Open <https://onedrive.live.com> (or your test tenant's OneDrive) as the demo account.
2. Drag the folder `~/eridi-demo/Lab Drive` from Finder into the browser to upload it.

### 3d. Connect, scan, search (terminal, window A)

1. **Sign in and choose the drive:**

   ```bash
   uv run eridi storage add-microsoft
   ```

   Your browser opens Microsoft's sign-in page. Sign in as the **demo** account and
   accept the permissions (all "read"). Back in Terminal:

   ✅ `Signed in as <demo account>`, then a numbered list (`1. OneDrive`). Type `1` and
   press Enter → `Added OneDrive.`

   No browser, or it fails to open? Use a code instead:
   `uv run eridi storage add-microsoft --device-code`, go to the address it shows, and
   type the code.

2. **Scan and search:**

   ```bash
   uv run eridi scan
   uv run eridi search "blocking buffer"
   uv run eridi storage list
   uv run eridi doctor
   ```

   ✅ The scan reports the OneDrive files as new; search finds
   `Lab Drive/Notes/lab_meeting_2026-10-14.md` and the PDF **from OneDrive** (source
   `OneDrive`). `storage list` shows `OneDrive  Microsoft`. `doctor` shows
   `Storage 'OneDrive': Microsoft, as <demo account> (search on).`

3. **Only changes are downloaded.** Run `uv run eridi scan` again.

   ✅ Nothing new or changed, and it's quick (it asks Microsoft only for what changed).

4. **An edit made in the cloud.** In OneDrive in the browser, open
   `Lab Drive/Protocols/Western_blot_blocking_v7.docx` in Word for the web, change
   `1 h at RT` to `3 h at RT`, close it, wait ~30 s, then:

   ```bash
   uv run eridi scan
   uv run eridi changes
   uv run eridi diff Western_blot_blocking_v7.docx
   ```

   Two files now have that name (your local demo copy and the OneDrive one), so Eridi
   asks which; pick the OneDrive one.

   ✅ `changes` lists it with `1 h at RT → 3 h at RT`; `diff` shows the lines.

5. **A rename and a delete in the cloud.** In OneDrive, rename the folder
   `Lab Drive/Papers` to `Papers 2026`, and delete `Lab Drive/Analysis/qc_summary.ipynb`.
   Then `uv run eridi scan`.

   ✅ The two papers are reported as moved, and the notebook as missing.

6. **A project spanning your computer and OneDrive.**

   ```bash
   uv run eridi project add-folder "Antibody validation 2026" "OneDrive"
   uv run eridi project show antibody
   ```

   ✅ Files list both `Lab Drive` (local) and `OneDrive` files.

### 3e. The web app flow

```bash
uv run eridi open
```

- [ ] **Storage** page → **Add Microsoft storage** → the Microsoft sign-in page opens in a
  new tab; finish signing in → back in Eridi: **Signed in as …** with `OneDrive`
  showing **Connected** (from step 3d).
- [ ] **Use another account** → **Sign in with a code** → instructions with a code
  appear on the page; complete them at microsoft.com/devicelogin.
- [ ] Search finds OneDrive files; their history opens.

### 3f. Optional: SharePoint, and the IT-approval message (test tenant only)

- **SharePoint:** in the test tenant, create a site, upload the demo folder to its
  Documents library, and **Follow** the site (star icon). Run
  `uv run eridi storage add-microsoft` again: the site's libraries are listed.
- **IT approval:** as the tenant admin, Entra → **Enterprise applications** → **Consent
  and permissions** → **User consent settings** → **Do not allow user consent**. Sign in
  to Eridi as an ordinary (non-admin) user of the tenant:

  ✅ Terminal and web app both say **Your university needs to approve this app. Send IT
  this link:** with an `…/adminconsent?client_id=…` link and `docs/IT_APPROVAL.md`.
  Opening that link as the admin and accepting lets the user sign in. Read
  `docs/IT_APPROVAL.md` as if you were IT: is anything unclear?

### 3g. Signed out, offline, and the live test

- **Offline:** turn Wi-Fi off, run `uv run eridi scan` (it retries for about 30 s
  before giving up).
  ✅ OneDrive is reported not available; nothing is marked missing
  (`uv run eridi search blocking` still finds the OneDrive files). Turn Wi-Fi on.
- **Live test:** prints your drives, the first files and the number of earlier versions
  Microsoft keeps of a file:

  ```bash
  ERIDI_LIVE_MSGRAPH=1 uv run pytest tests/test_msgraph_live.py -s
  ```

  (Version history is read by the connector, but not yet shown in `eridi history`.)

---

## 4. Clean up

```bash
rm -rf ~/eridi-demo
unset ERIDI_HOME ERIDI_MS_CLIENT_ID
```

- Sign-in tokens are in your keychain under **eridi-microsoft**: on macOS, open
  **Keychain Access**, search `eridi-microsoft`, delete the entries. On Windows, **Credential
  Manager** → Windows Credentials.
- Delete the `Eridi demo` app registration in Entra and the demo files in OneDrive if you
  like.

## What to write down

For each ✅ that didn't happen: the command, what you saw (copy the text), and your
operating system. Also note anything confusing in wording or layout, even if it worked.

---

## Windows

Use PowerShell. The differences:

| macOS | Windows PowerShell |
|---|---|
| `export ERIDI_HOME=~/eridi-demo/computer-A` | `$env:ERIDI_HOME = "$HOME\eridi-demo\computer-A"` |
| `export ERIDI_MS_CLIENT_ID=<id>` | `$env:ERIDI_MS_CLIENT_ID = "<id>"` |
| `~/eridi-demo/"Lab Drive"` | `"$HOME\eridi-demo\Lab Drive"` |
| `cp -R A B` | `Copy-Item -Recurse "A" "B"` |
| `mkdir -p X` | `New-Item -ItemType Directory "X"` |
| `ERIDI_LIVE_MSGRAPH=1 uv run pytest …` | `$env:ERIDI_LIVE_MSGRAPH = "1"; uv run pytest …` |
| `rm -rf ~/eridi-demo` | `Remove-Item -Recurse -Force "$HOME\eridi-demo"` |

For the conflict-copy check (M9 step 5):

```powershell
$F = Get-ChildItem "$HOME\eridi-demo\Lab Record\entries" -Recurse -Filter *.json | Select-Object -First 1
Copy-Item $F.FullName ($F.FullName -replace '\.json$', ' (1).json')
uv run eridi doctor
Remove-Item ($F.FullName -replace '\.json$', ' (1).json')
```
