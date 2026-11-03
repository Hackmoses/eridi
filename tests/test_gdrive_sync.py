"""M12.3: Drive throttling and retries, paging, a resumable first listing, expired change
tokens, and the cursor kept in the index between scans."""

import json
from pathlib import Path

import httpx
import pytest

from eridi.connectors import gdrive
from eridi.connectors.gdrive import DriveClient, DriveError, GDriveConnector, root_id_for
from eridi.connectors.tree import DeltaCache, MemoryDeltaCache
from eridi.core.index.cloud import IndexDeltaCache
from eridi.core.index.db import open_index
from gdrive_fake import MY_DRIVE, TOKEN, FakeDrive, fixture

ROOT = root_id_for(MY_DRIVE)
SOP = "Protocols 2026/Cell culture SOP.gdoc"


def client(drive: FakeDrive, sleeps: list[float], attempts: int = 6) -> DriveClient:
    http = httpx.Client(transport=httpx.MockTransport(drive.handle), follow_redirects=True)
    return DriveClient(lambda: TOKEN, http=http, sleep=sleeps.append, max_attempts=attempts)


def connector(
    drive: FakeDrive, cache: DeltaCache | None = None, sleeps: list[float] | None = None
) -> GDriveConnector:
    http_client = client(drive, sleeps if sleeps is not None else [])
    return GDriveConnector(ROOT, f"my-drive:{MY_DRIVE}", http_client, cache=cache)


def test_throttling_honors_retry_after_and_403_rate_limits() -> None:
    drive = FakeDrive()
    drive.queue("/files", httpx.Response(429, headers={"Retry-After": "7"}))
    drive.queue("/files", httpx.Response(403, json=fixture("error_rate_limit")))
    sleeps: list[float] = []
    files = list(connector(drive, sleeps=sleeps).list_files())
    assert len(files) == 5
    assert sleeps == [7.0, 2.0]  # Retry-After, then backoff for Google's 403 rate limit


def test_forbidden_is_not_retried_and_says_why() -> None:
    drive = FakeDrive()
    drive.queue(
        "/files", httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    )
    sleeps: list[float] = []
    with pytest.raises(DriveError, match="Google refused access"):
        list(connector(drive, sleeps=sleeps).list_files())
    assert sleeps == []


def test_busy_google_gives_up_after_the_last_attempt() -> None:
    drive = FakeDrive()
    for _ in range(3):
        drive.queue("/about", httpx.Response(503))
    sleeps: list[float] = []
    with pytest.raises(DriveError, match="Google is busy"):
        client(drive, sleeps, attempts=3).get("/about")
    assert sleeps == [1.0, 2.0]


def test_expired_sign_in_says_sign_in_again() -> None:
    drive = FakeDrive()
    drive.queue("/files", httpx.Response(401, json={"error": {"code": 401}}))
    with pytest.raises(DriveError, match="add-google"):
        list(connector(drive).list_files())


def test_interrupted_first_listing_resumes_from_the_saved_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gdrive, "SAVE_EVERY_PAGES", 1)
    drive = FakeDrive()
    original = drive.handle
    fail_page2 = {"on": True}

    def flaky(request: httpx.Request) -> httpx.Response:
        if fail_page2["on"] and dict(request.url.params).get("pageToken") == "p2":
            return httpx.Response(500)
        return original(request)

    drive.handle = flaky  # type: ignore[method-assign]
    cache = MemoryDeltaCache()
    with pytest.raises(DriveError):
        list(connector(drive, cache).list_files())
    cursor, items = cache.load(ROOT)
    assert json.loads(cursor or "") == {"start": "100", "list": "p2"}
    assert "DOC1" in items  # page 1 was kept

    fail_page2["on"] = False
    before = len(drive.requests)
    files = [f.path for f in connector(drive, cache).list_files()]
    assert SOP in files and "lab_meeting_2026-10-14.md" in files
    listed = [r for r in drive.requests[before:] if r.url.path.endswith("/files")]
    assert [dict(r.url.params).get("pageToken") for r in listed] == ["p2"]  # page 1 not again
    assert json.loads(cache.load(ROOT)[0] or "") == {"changes": "100"}


def test_expired_change_token_lists_the_drive_again() -> None:
    drive = FakeDrive()
    cache = MemoryDeltaCache()
    list(connector(drive, cache).list_files())
    cache.save(ROOT, json.dumps({"changes": "999"}), [], [], reset=False)  # Google forgot it
    files = [f.path for f in connector(drive, cache).list_files()]
    assert SOP in files
    assert json.loads(cache.load(ROOT)[0] or "") == {"changes": "100"}
    assert sum(1 for p in drive.paths() if p.startswith("/files?")) == 4  # listed twice


def test_tree_and_cursor_kept_in_the_index(tmp_path: Path) -> None:
    drive = FakeDrive()
    index = tmp_path / "index.sqlite"
    open_index(index).close()
    list(connector(drive, IndexDeltaCache(index)).list_files())
    cursor, items = IndexDeltaCache(index).load(ROOT)
    assert json.loads(cursor or "") == {"changes": "100"}
    assert items["DOC1"].mime == "application/vnd.google-apps.document"  # kept for exports
    before = len(drive.requests)
    again = connector(drive, IndexDeltaCache(index))  # a new scan, another process
    assert [f.path for f in again.list_files()]
    assert [r.url.path for r in drive.requests[before:]] == ["/drive/v3/changes"]
