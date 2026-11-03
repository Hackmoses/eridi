"""M10.3: throttling and retries, paging, and a resumable delta cursor kept in the index."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from eridi.connectors import msgraph
from eridi.connectors.msgraph import GraphError, MemoryDeltaCache, MsGraphConnector, root_id_for
from eridi.core.index.cloud import IndexDeltaCache
from eridi.core.index.db import open_index
from msgraph_fake import DRIVE, FakeGraph, fixture

DELTA = f"/drives/{DRIVE}/root/delta"
PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"


def throttled(retry_after: str | None = None, status: int = 429) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, headers=headers, json=fixture("error_throttled"))


def drive(graph: FakeGraph, cache: msgraph.DeltaCache) -> MsGraphConnector:
    return MsGraphConnector(root_id_for(DRIVE), DRIVE, graph.client(), cache=cache)


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


def client_with_sleeps(
    graph: FakeGraph, sleeps: list[float], attempts: int = 6
) -> msgraph.GraphClient:
    client = graph.client()
    client.sleep = sleeps.append
    client.max_attempts = attempts
    return client


def test_throttling_honors_retry_after(graph: FakeGraph) -> None:
    graph.queue(DELTA, throttled("7"))
    graph.queue(DELTA, throttled("3", status=503))
    sleeps: list[float] = []
    c = MsGraphConnector(root_id_for(DRIVE), DRIVE, client_with_sleeps(graph, sleeps))
    assert len(list(c.list_files())) == 2
    assert sleeps == [7.0, 3.0]


def test_retry_after_as_a_date_and_capped(graph: FakeGraph) -> None:
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    graph.queue("/me/drive", throttled(later))
    graph.queue("/me/drive", throttled("86400"))  # a day: capped
    sleeps: list[float] = []
    client_with_sleeps(graph, sleeps).get("/me/drive")
    assert 25 <= sleeps[0] <= 31
    assert sleeps[1] == msgraph.MAX_WAIT


def test_backoff_without_retry_after_and_giving_up(graph: FakeGraph) -> None:
    for _ in range(3):
        graph.queue("/me/drive", throttled(status=502))
    sleeps: list[float] = []
    with pytest.raises(GraphError, match="Microsoft is busy"):
        client_with_sleeps(graph, sleeps, attempts=3).get("/me/drive")
    assert sleeps == [1.0, 2.0]


def test_dropped_connections_are_retried(graph: FakeGraph) -> None:
    calls = {"n": 0}
    original = graph.handle

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return original(request)

    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(flaky), follow_redirects=True)
    client = msgraph.GraphClient(lambda: "fake-access-token", http=http, sleep=sleeps.append)
    assert client.get("/me/drive")["id"] == "b!OkaforDrive01"
    assert sleeps == [1.0]
    http_down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(
        httpx.ConnectError("offline"))))  # fmt: skip
    offline = msgraph.GraphClient(lambda: "t", http=http_down, sleep=lambda s: None)
    with pytest.raises(GraphError, match="Check the internet connection"):
        offline.get("/me/drive")


def test_paging_follows_next_links(graph: FakeGraph) -> None:
    pages = list(graph.client().pages(DELTA))
    assert len(pages) == 2
    assert "@odata.deltaLink" in pages[-1]


def test_cursor_is_kept_in_the_index_across_scans(graph: FakeGraph, tmp_path: Path) -> None:
    index = tmp_path / "index.sqlite"
    open_index(index).close()
    cache = IndexDeltaCache(index)
    first = MsGraphConnector(root_id_for(DRIVE), DRIVE, graph.client(), cache=cache)
    assert len(list(first.list_files())) == 2
    cursor, items = cache.load(root_id_for(DRIVE))
    assert cursor is not None and cursor.endswith("token=t1")
    assert {"ROOT", "FOLDER1", "FILE1", "FILE2"} <= set(items)

    graph.requests.clear()
    second = MsGraphConnector(
        root_id_for(DRIVE), DRIVE, graph.client(), cache=IndexDeltaCache(index)
    )
    assert [f.path for f in second.list_files()] == [PROTOCOL, "lab_meeting_2026-10-14.md"]
    assert graph.paths() == [f"{DELTA}?token=t1"]  # only what changed, not the whole drive

    IndexDeltaCache(index).forget(root_id_for(DRIVE))
    assert IndexDeltaCache(index).load(root_id_for(DRIVE)) == (None, {})


def test_interrupted_listing_resumes(
    graph: FakeGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msgraph, "SAVE_EVERY_PAGES", 1)
    index = tmp_path / "index.sqlite"
    open_index(index).close()
    graph.queue(f"{DELTA}?token=page2", httpx.Response(400, json={"error": {"code": "x"}}))
    first = MsGraphConnector(
        root_id_for(DRIVE), DRIVE, graph.client(), cache=IndexDeltaCache(index)
    )
    with pytest.raises(GraphError):
        list(first.list_files())
    cursor, items = IndexDeltaCache(index).load(root_id_for(DRIVE))
    assert cursor is not None and cursor.endswith("token=page2")  # page 1 is kept
    assert "FILE1" in items

    graph.requests.clear()
    resumed = MsGraphConnector(
        root_id_for(DRIVE), DRIVE, graph.client(), cache=IndexDeltaCache(index)
    )
    assert [f.path for f in resumed.list_files()] == [PROTOCOL, "lab_meeting_2026-10-14.md"]
    assert graph.paths() == [f"{DELTA}?token=page2"]


def test_expired_cursor_lists_again_from_the_start(graph: FakeGraph) -> None:
    cache = MemoryDeltaCache()
    list(MsGraphConnector(root_id_for(DRIVE), DRIVE, graph.client(), cache=cache).list_files())
    cache.data[root_id_for(DRIVE)][1]["STALE"] = msgraph.CloudItem(
        "STALE", "ROOT", "gone-long-ago.txt", False, 1, 1.0
    )
    graph.queue(f"{DELTA}?token=t1", httpx.Response(410, json=fixture("error_resync")))
    graph.requests.clear()
    c = MsGraphConnector(root_id_for(DRIVE), DRIVE, graph.client(), cache=cache)
    assert [f.path for f in c.list_files()] == [PROTOCOL, "lab_meeting_2026-10-14.md"]
    assert graph.paths() == [f"{DELTA}?token=t1", DELTA, f"{DELTA}?token=page2"]
    assert "STALE" not in cache.data[root_id_for(DRIVE)][1]  # replaced by the fresh listing


def test_index_rebuild_forgets_cloud_trees(tmp_path: Path) -> None:
    index = tmp_path / "index.sqlite"
    open_index(index).close()
    IndexDeltaCache(index).save(
        "ms:x", "cursor", [msgraph.CloudItem("ROOT", None, "root", True, is_root=True)], [],
        reset=True,
    )  # fmt: skip
    conn = open_index(index)
    from eridi.core.index.db import create_schema, drop_all

    drop_all(conn)
    create_schema(conn)
    conn.close()
    assert IndexDeltaCache(index).load("ms:x") == (None, {})
