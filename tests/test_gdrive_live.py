"""M12: a live check against a real Google account. Skipped unless asked for:

    ERIDI_LIVE_GDRIVE=1 ERIDI_GOOGLE_CLIENT_ID=<id> ERIDI_GOOGLE_CLIENT_SECRET=<secret> \\
        uv run pytest tests/test_gdrive_live.py -s

It signs in in your browser, lists your drives, and reads the first few files of My Drive
(exporting Google Docs as text). Read-only, like everything Eridi does.
"""

import importlib
import os

import pytest

from eridi.connectors.gauth import GoogleAccount, GoogleAuth
from eridi.connectors.gdrive import DriveClient, GDriveConnector, list_drives, root_id_for, whoami

pytestmark = pytest.mark.skipif(
    os.environ.get("ERIDI_LIVE_GDRIVE") != "1",
    reason="live Google test (set ERIDI_LIVE_GDRIVE=1)",
)


@pytest.fixture
def real_keyring() -> None:
    """Use the OS keychain for real, so the next run stays signed in."""
    import keyring

    importlib.reload(keyring)


def test_live_sign_in_list_and_read(real_keyring: None) -> None:
    auth = GoogleAuth()

    def identify(token: str) -> GoogleAccount:
        return GoogleAccount(*whoami(DriveClient(lambda: token)))

    account = auth.sign_in(identify)
    print(f"Signed in as {account.email}")
    client = DriveClient(lambda: auth.token(account.id))
    drives = list_drives(client)
    for d in drives:
        print(f"  {d.label} ({d.kind})")
    drive = drives[0]
    connector = GDriveConnector(root_id_for(drive.id), drive.location, client)
    files = []
    for remote in connector.list_files():
        files.append(remote)
        if len(files) >= 5:
            break
    print(f"First files in {drive.label}: {[f.path for f in files]}")
    if files:
        data = connector.open_bytes(files[0].path)
        print(f"Read {files[0].path}: {len(data)} bytes; "
              f"{len(connector.versions(files[0].path))} earlier versions")  # fmt: skip
