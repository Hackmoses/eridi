"""M10: a live check against a real Microsoft account. Skipped unless asked for:

    ERIDI_LIVE_MSGRAPH=1 ERIDI_MS_CLIENT_ID=<app id> uv run pytest tests/test_msgraph_live.py -s

It signs in (browser, or a code with ERIDI_LIVE_DEVICE_CODE=1), lists your drives, and
reads the first few files of the first one. Read-only, like everything Eridi does.
"""

import os

import pytest

from eridi.connectors.msauth import MicrosoftAuth
from eridi.connectors.msgraph import GraphClient, MsGraphConnector, list_drives, root_id_for

pytestmark = pytest.mark.skipif(
    os.environ.get("ERIDI_LIVE_MSGRAPH") != "1",
    reason="live Microsoft test (set ERIDI_LIVE_MSGRAPH=1)",
)


@pytest.fixture
def real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the OS keychain for real, so the next run stays signed in."""
    import importlib

    import keyring

    importlib.reload(keyring)


def test_live_sign_in_list_and_read(real_keyring: None) -> None:
    auth = MicrosoftAuth()
    account = auth.sign_in(device_code=os.environ.get("ERIDI_LIVE_DEVICE_CODE") == "1")
    print(f"Signed in as {account.username}")
    client = GraphClient(lambda: auth.token(account.id))
    drives = list_drives(client)
    assert drives, "no drives"
    for d in drives:
        print(f"  {d.label} ({d.kind})")
    drive = drives[0]
    connector = MsGraphConnector(root_id_for(drive.id), drive.id, client)
    files = []
    for remote in connector.list_files():
        files.append(remote)
        if len(files) >= 5:
            break
    print(f"First files in {drive.label}: {[f.path for f in files]}")
    if files:
        data = connector.open_bytes(files[0].path)
        assert len(data) == files[0].size
        print(
            f"Read {files[0].path}: {len(data)} bytes; {len(connector.versions(files[0].path))} "
            "earlier versions"
        )
