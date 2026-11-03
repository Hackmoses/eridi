"""M11.2: `Connector.save()` for local folders and Microsoft, and never overwriting unasked."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import eridi.core.service as service_module
from conftest import SampleLab
from eridi.connectors.base import ConnectorError, NameTakenInStorageError, SavingConnector
from eridi.connectors.local import LocalConnector
from eridi.connectors.msauth import WRITE_SCOPES, MicrosoftAuthError, WriteAccessNeededError
from eridi.connectors.msgraph import GRAPH, UPLOAD_CHUNK, GraphClient, MsGraphConnector
from eridi.core.service import Eridi, SaveApprovalNeededError
from msgraph_fake import DRIVE, FakeGraph
from test_microsoft_storage import ACCOUNT, FakeAuth
from test_msauth import FakeApp, auth_with

# --- local folders ---


def test_local_save_never_overwrites_unless_asked(tmp_path: Path) -> None:
    (tmp_path / "Protocols").mkdir()
    existing = tmp_path / "Protocols" / "v7.txt"
    existing.write_bytes(b"original")
    connector = LocalConnector(tmp_path, "root_A")
    assert isinstance(connector, SavingConnector)
    with pytest.raises(NameTakenInStorageError):
        connector.save("Protocols/v7.txt", b"new")
    assert existing.read_bytes() == b"original"
    saved = connector.save("Protocols/v7.txt", b"new", overwrite=True)
    assert existing.read_bytes() == b"new" and saved.size == 3
    new = connector.save("Results/2026/run.csv", b"a,b\n")  # folders are created
    assert new.path == "Results/2026/run.csv" and connector.exists("Results/2026/run.csv")
    assert not list(tmp_path.rglob("*.tmp"))


def test_local_save_stays_inside_the_folder(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    connector = LocalConnector(root, "root_A")
    for bad in ("../escape.txt", "/etc/passwd", "C:\\Windows\\x.txt"):
        with pytest.raises((ValueError, ConnectorError)):
            connector.save(bad, b"x")
    assert not (tmp_path / "escape.txt").exists()


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive", save=True)
        eridi.create_project("Blots", home_root_id=root.id, folders=[f"{root.id}:Protocols"])
        yield eridi


def test_name_taken_asks_then_new_name_or_replace(lab: Eridi, sample_lab: SampleLab) -> None:
    target = sample_lab.root / "Protocols" / "plan.txt"
    target.write_bytes(b"someone else's plan\n")
    job = lab.save_file(b"my plan\n", "plan.txt", project="Blots")
    assert job.state == "needs_confirmation"
    assert target.read_bytes() == b"someone else's plan\n"  # untouched

    renamed = lab.retry_save(job.id, "rename")
    assert renamed.state == "uploaded" and renamed.uploaded_path == "Protocols/plan (2).txt"
    assert (sample_lab.root / "Protocols" / "plan (2).txt").read_bytes() == b"my plan\n"

    again = lab.save_file(b"my plan, v2\n", "plan.txt", project="Blots")
    replaced = lab.retry_save(again.id, "replace")
    assert replaced.state == "uploaded" and target.read_bytes() == b"my plan, v2\n"


def test_save_as_new_name_up_front(lab: Eridi, sample_lab: SampleLab) -> None:
    (sample_lab.root / "Protocols" / "a.txt").write_bytes(b"1")
    job = lab.save_file(b"2", "a.txt", project="Blots", on_exists="rename")
    assert job.uploaded_path == "Protocols/a (2).txt"


# --- Microsoft ---


class UploadGraph(FakeGraph):
    """The recorded Graph, plus uploads: simple PUTs and upload sessions."""

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, bytes] = {}
        self.chunks: list[str] = []
        self.write_tokens: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "upload.example":
            assert "Authorization" not in request.headers  # never sent to the upload URL
            self.requests.append(request)
            self.chunks.append(request.headers["Content-Range"])
            _start, end_total = request.headers["Content-Range"].removeprefix("bytes ").split("-")
            end, total = end_total.split("/")
            path = url.path.strip("/")
            buffer = self.files.get(path, b"") + request.content
            self.files[path] = buffer
            if int(end) + 1 < int(total):
                return httpx.Response(202, json={"nextExpectedRanges": [f"{int(end) + 1}-"]})
            return httpx.Response(201, json={"size": len(buffer), "name": path})
        marker = f"/drives/{DRIVE}/root:/"
        raw = str(url).removeprefix(GRAPH)
        if marker in raw:
            self.requests.append(request)
            self.write_tokens.append(request.headers.get("Authorization", ""))
            path = url.path.split("/root:/", 1)[1].split(":", 1)[0]
            if request.method == "GET":
                if path in self.files:
                    return httpx.Response(200, json={"id": "X", "name": path})
                return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
            if request.method == "PUT":
                behavior = url.params.get("@microsoft.graph.conflictBehavior")
                if path in self.files and behavior == "fail":
                    return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
                self.files[path] = request.content
                return httpx.Response(201, json={
                    "size": len(request.content), "lastModifiedDateTime": "2026-11-03T10:00:00Z",
                })  # fmt: skip
            if request.method == "POST":
                body = json.loads(request.content)
                behavior = body["item"]["@microsoft.graph.conflictBehavior"]
                if path in self.files and behavior == "fail":
                    return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
                self.files.pop(path, None)
                return httpx.Response(200, json={"uploadUrl": f"https://upload.example/{path}"})
        return super().handle(request)

    def writer(self, token: str = "write-token") -> GraphClient:  # noqa: S107 - test value
        http = httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)
        return GraphClient(lambda: token, http=http, sleep=lambda s: None)


def test_graph_save_small_file_without_overwriting() -> None:
    graph = UploadGraph()
    connector = MsGraphConnector(f"ms:{DRIVE}", DRIVE, graph.client(), writer=graph.writer())
    assert isinstance(connector, SavingConnector)
    saved = connector.save("Protocols 2026/new plan.txt", b"plan")
    assert saved.size == 4 and graph.files["Protocols 2026/new plan.txt"] == b"plan"
    assert graph.write_tokens[-1] == "Bearer write-token"  # the write token, not the read one
    assert connector.exists("Protocols 2026/new plan.txt")
    assert not connector.exists("nope.txt")
    with pytest.raises(NameTakenInStorageError):
        connector.save("Protocols 2026/new plan.txt", b"other")
    assert graph.files["Protocols 2026/new plan.txt"] == b"plan"
    connector.save("Protocols 2026/new plan.txt", b"other", overwrite=True)
    assert graph.files["Protocols 2026/new plan.txt"] == b"other"


def test_graph_save_large_file_in_an_upload_session() -> None:
    graph = UploadGraph()
    connector = MsGraphConnector(f"ms:{DRIVE}", DRIVE, graph.client(), writer=graph.writer())
    data = bytes(range(256)) * (5 * 1024 * 1024 // 256)  # 5 MB: over the simple-upload limit
    saved = connector.save("big.bin", data)
    assert graph.files["big.bin"] == data and saved.size == len(data)
    assert graph.chunks[0] == f"bytes 0-{UPLOAD_CHUNK - 1}/{len(data)}"
    assert len(graph.chunks) == -(-len(data) // UPLOAD_CHUNK)


def test_graph_save_without_writer_refuses() -> None:
    graph = UploadGraph()
    connector = MsGraphConnector(f"ms:{DRIVE}", DRIVE, graph.client())
    with pytest.raises(ConnectorError, match="off"):
        connector.save("x.txt", b"x")
    assert graph.files == {}


def test_write_token_uses_write_scopes_only_when_asked() -> None:
    app = FakeApp("client", None)
    auth = auth_with(app)
    auth.sign_in()
    scopes: list[Any] = []
    app.acquire_token_silent = lambda s, account: scopes.append(s) or {"access_token": "AT"}  # type: ignore[method-assign]
    auth.token("uid.tid")
    auth.token("uid.tid", write=True)
    assert scopes == [["Files.Read.All", "Sites.Read.All", "User.Read"], WRITE_SCOPES]

    app.acquire_token_silent = lambda s, account: None  # type: ignore[method-assign]
    with pytest.raises(WriteAccessNeededError, match="allow Eridi to write"):
        auth.token("uid.tid", write=True)


def test_allow_saving_asks_for_write_scopes_for_the_same_account() -> None:
    app = FakeApp("client", None)
    auth = auth_with(app)
    auth.sign_in()
    account = auth.allow_saving("uid.tid")
    assert account.id == "uid.tid"
    kind, (scopes, kwargs) = app.calls[-1]
    assert kind == "interactive" and scopes == WRITE_SCOPES
    assert kwargs["login_hint"] == "a.okafor@uni.example"
    with pytest.raises(MicrosoftAuthError, match="Sign in with that account"):
        auth.allow_saving("someone-else")


class WriteFakeAuth(FakeAuth):
    def __init__(self) -> None:
        super().__init__()
        self.can_write = False
        self.write_calls = 0

    def token(self, account_id: str, *, write: bool = False) -> str:
        self.write_calls += write
        if write and not self.can_write:
            raise WriteAccessNeededError(ACCOUNT.username)
        return super().token(account_id)

    def allow_saving(self, account_id: str, **kwargs: Any) -> Any:
        self.can_write = True
        return ACCOUNT


@pytest.fixture
def ms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Eridi, UploadGraph]]:
    graph = UploadGraph()
    fake = WriteFakeAuth()
    monkeypatch.setattr(service_module, "MicrosoftAuth", lambda *a, **k: fake)

    def client(token: Callable[[], str]) -> GraphClient:
        http = httpx.Client(transport=httpx.MockTransport(graph.handle), follow_redirects=True)
        return GraphClient(token, http=http, sleep=lambda s: None)

    monkeypatch.setattr(service_module, "GraphClient", client)
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.microsoft_sign_in()
        eridi.add_microsoft_root(ACCOUNT.id, DRIVE)
        yield eridi, graph


def test_turning_save_on_for_microsoft_needs_write_approval(
    ms: tuple[Eridi, UploadGraph],
) -> None:
    eridi, graph = ms
    root_id = f"ms:{DRIVE}"
    with pytest.raises(SaveApprovalNeededError, match="allow Eridi to write"):
        eridi.update_root(root_id, save=True)
    assert not eridi.get_root(root_id).save
    assert eridi.allow_microsoft_saving(root_id).save

    project = eridi.create_project("Blots", home_root_id=root_id)
    job = eridi.save_file(b"lane,signal\n", "run1.csv", project=project.id)
    assert job.state == "uploaded", job.last_error
    assert graph.files["run1.csv"] == b"lane,signal\n"
    assert eridi.microsoft.write_calls  # type: ignore[attr-defined]  # a write token was used


def test_microsoft_write_access_withdrawn_blocks_instead_of_retrying(
    ms: tuple[Eridi, UploadGraph], monkeypatch: pytest.MonkeyPatch
) -> None:
    eridi, graph = ms
    root_id = f"ms:{DRIVE}"
    eridi.allow_microsoft_saving(root_id)
    job = eridi.save_file(b"x", "x.txt", storage=root_id, upload=False)
    eridi.microsoft.can_write = False  # type: ignore[attr-defined]
    [result] = eridi.process_outbox()
    assert result.state == "blocked" and "allow Eridi to write" in (result.last_error or "")
    assert graph.files == {}
    assert job.id == result.id
