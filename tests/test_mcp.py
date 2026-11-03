"""M6: the MCP server, through an in-process MCP client."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from mcp import Client
from typer.testing import CliRunner

from conftest import SampleLab
from eridi.cli.main import app as cli
from eridi.core.config import save_config
from eridi.core.service import Eridi
from eridi.mcp.server import MAX_OUTPUT_CHARS, TRUNCATED_NOTE, build_server, fit

PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
READ_TOOLS = {"search", "file_history", "get_entry", "list_recent", "pending_changes", "show_diff"}


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> Iterator[Eridi]:
    with Eridi() as eridi:
        eridi.init("A. Okafor", "a@example.edu", tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        doc = Document(str(sample_lab.protocol_docx))
        doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
        doc.save(str(sample_lab.protocol_docx))
        eridi.scan()
        yield eridi


def call(eridi: Eridi, tool: str, **arguments: Any) -> tuple[bool, Any]:
    """Call one tool; return (is_error, parsed JSON or error text)."""

    async def go() -> tuple[bool, Any]:
        async with Client(build_server(eridi)) as client:
            result = await client.call_tool(tool, arguments)
            text = "".join(getattr(c, "text", "") for c in result.content)
            if result.is_error:
                return True, text
            return False, json.loads(text)

    return asyncio.run(go())


def tools(eridi: Eridi) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with Client(build_server(eridi)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    return asyncio.run(go())


def test_read_only_tools_are_listed(lab: Eridi) -> None:
    listed = tools(lab)
    assert set(listed) == READ_TOOLS
    assert all(t.annotations.read_only_hint for t in listed.values())


def test_search_cites_sources(lab: Eridi) -> None:
    note = lab.add_note("Switched to BSA because milk gave high background.", file=PROTOCOL)
    error, data = call(lab, "search", query="milk background")
    assert not error
    entry = next(r for r in data["results"] if r["type"] == "record entry")
    assert entry["entry_id"] == note.id
    assert entry["author"] == "A. Okafor"
    assert entry["created_at"] == note.created_at
    assert entry["path"] == PROTOCOL and entry["storage"] == "Lab Drive"
    assert "[milk]" in entry["snippet"]
    file_hit = next(r for r in data["results"] if r["type"] == "file")
    assert file_hit["file_id"].endswith(":" + file_hit["path"])


def test_pending_changes_diff_and_history(lab: Eridi) -> None:
    error, data = call(lab, "pending_changes")
    assert not error
    [change] = data["changes"]
    assert change["path"] == PROTOCOL
    assert change["summary"] == "Primary antibody incubation: 1 h at RT → 2 h at RT"

    error, d = call(lab, "show_diff", file="blocking_v7")
    assert not error and d["available"]
    assert "+ Primary antibody incubation: 2 h at RT." in d["diff"]

    note = lab.add_note("Longer incubation for the new lot.", file=PROTOCOL, outcome="worked")
    error, h = call(lab, "file_history", file=change["file_id"])
    assert not error
    assert h["file"]["path"] == PROTOCOL
    assert [v["current"] for v in h["versions"]] == [False, True]
    [e] = h["entries"]
    assert e["id"] == note.id and e["outcome"] == "worked" and e["state"] == "current"
    assert e["change"] == "Primary antibody incubation: 1 h at RT → 2 h at RT"


def test_get_entry_and_recent(lab: Eridi) -> None:
    first = lab.add_note("First.", tags=["lot"])
    second = lab.add_note("Second, corrected.", supersedes=first.id)
    error, e = call(lab, "get_entry", entry_id=first.id)
    assert not error and e["state"] == "superseded" and e["tags"] == ["lot"]
    error, r = call(lab, "list_recent", limit=5)
    assert [x["id"] for x in r["entries"]] == [second.id]
    error, r = call(lab, "list_recent", kind="decision")
    assert r["entries"] == []


def test_errors_are_tool_errors(lab: Eridi) -> None:
    error, text = call(lab, "get_entry", entry_id="01JC8Z9K3V5W6X7Y8Z9A0B1C2D")
    assert error and "no record entry" in text
    error, text = call(lab, "file_history", file="Papers/")
    assert error and "matches 2 files" in text


def test_write_tool_refused_when_disabled(lab: Eridi) -> None:
    error, _ = call(lab, "add_note", text="Should not be saved.")
    assert error
    assert lab.recent() == []


def test_agent_notes_when_writes_are_allowed(lab: Eridi) -> None:
    save_config(replace(lab.config, mcp_allow_writes=True), lab.config_file)
    with Eridi() as eridi:
        assert "add_note" in tools(eridi)
        error, data = call(eridi, "add_note", text="Assistant summary of the lot change.",
                           file=PROTOCOL, outcome="unknown")  # fmt: skip
        assert not error
        saved = data["saved"]
        assert saved["author"]["kind"] == "agent"
        entry = eridi.get_entry(saved["id"])
        assert entry.author.kind == "agent"
        assert entry.author.name == "AI assistant (for A. Okafor)"
        assert entry.change is not None  # it explained the pending change
        error, text = call(eridi, "add_note", text="x", outcome="great")
        assert error and "Outcome must be one of" in text


def test_output_is_capped(lab: Eridi, sample_lab: SampleLab) -> None:
    for i in range(40):
        lab.add_note(f"Note {i} about blocking. " + "Long detail. " * 400)
    error, data = call(lab, "list_recent", limit=100)
    assert not error
    assert data["note"] == TRUNCATED_NOTE
    assert len(json.dumps(data, ensure_ascii=False)) <= MAX_OUTPUT_CHARS
    assert data["entries"]
    assert len(data["entries"]) < 40


def test_fit_keeps_small_output_whole() -> None:
    small = {"results": [{"text": "short"}]}
    assert json.loads(fit(small)) == small
    huge = {"text": "x" * 100_000, "items": list(range(10))}
    out = fit(huge, limit=2_000)
    assert len(out) <= 2_000
    assert "shortened" in out


def test_mcp_config_command(isolated_home: Path) -> None:
    result = CliRunner().invoke(cli, ["mcp", "config"])
    assert result.exit_code == 0, result.output
    start = result.output.index("{")
    end = result.output.index("\n}\n") + 2
    snippet = json.loads(result.output[start:end])
    server = snippet["mcpServers"]["eridi"]
    assert server["args"][-1] == "mcp"
    assert "claude mcp add eridi --" in result.output
    assert "allow_writes = true" in result.output


def test_eridi_mcp_over_stdio(lab: Eridi, isolated_home: Path) -> None:
    """`eridi mcp` as a real subprocess, the way Claude Desktop starts it."""
    import os
    import sys

    from mcp.client.stdio import StdioServerParameters

    note = lab.add_note("Stdio check: blocking in BSA.")
    lab.close()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "eridi", "mcp"],
        env={**os.environ, "ERIDI_HOME": str(isolated_home)},
    )

    async def go() -> Any:
        async with Client(params) as client:
            result = await client.call_tool("search", {"query": "stdio check"})
            return json.loads("".join(getattr(c, "text", "") for c in result.content))

    data = asyncio.run(go())
    assert data["results"][0]["entry_id"] == note.id
