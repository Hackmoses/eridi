"""The `eridi` command. Every command is a thin wrapper over `eridi.core.service.Eridi`."""

import contextlib
import json
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from eridi import __version__
from eridi.core.changes import FileDiff
from eridi.core.config import ConfigError
from eridi.core.index.db import IndexUnavailableError
from eridi.core.index.search import SearchHit
from eridi.core.models import OUTCOMES, Entry, FileInfo, parse_timestamp
from eridi.core.record.store import RecordFolderError
from eridi.core.scan import ScanProgress
from eridi.core.service import (
    AmbiguousFileError,
    Eridi,
    EridiError,
    NotInitializedError,
    file_id,
)

app = typer.Typer(
    name="eridi",
    help="Find your lab's files, see what changed, and record why.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
storage_app = typer.Typer(help="Connect and list storage folders.", no_args_is_help=True)
index_app = typer.Typer(help="Maintain the search index.", no_args_is_help=True)
mcp_app = typer.Typer(
    help="Let AI assistants (Claude and other MCP clients) use the record. "
    "With no subcommand, runs the MCP server over stdio.",
    invoke_without_command=True,
)
app.add_typer(storage_app, name="storage")
app.add_typer(index_app, name="index")
app.add_typer(mcp_app, name="mcp")

OUTCOME_HELP = f"Did it work? One of: {', '.join(OUTCOMES)}."


def console() -> Console:
    return Console(highlight=False, soft_wrap=True)


def err_console() -> Console:
    return Console(highlight=False, soft_wrap=True, stderr=True)


@contextlib.contextmanager
def session() -> Iterator[Eridi]:
    """Open Eridi, and turn known problems into a message and exit code 1."""
    try:
        with Eridi() as eridi:
            yield eridi
    except AmbiguousFileError as exc:
        err_console().print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    except (EridiError, ConfigError, RecordFolderError, IndexUnavailableError) as exc:
        err_console().print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        err_console().print("Stopped. Work done so far is kept; run the command again to go on.")
        raise typer.Exit(130) from None


@contextlib.contextmanager
def scan_progress() -> Iterator[Callable[[ScanProgress], None]]:
    """A one-line live status for long scans: storage, files done of total, current file."""
    out = console()
    started = time.monotonic()
    with out.status("Scanning…") as status:

        def update(p: ScanProgress) -> None:
            name = p.path if len(p.path) <= 50 else "…" + p.path[-49:]
            label = escape(p.root.label)
            if p.phase == "listing":
                status.update(f"Listing {label}: {p.done:,} files · {escape(name)}")
                return
            total = p.total or 0
            eta = ""
            elapsed = time.monotonic() - started
            if elapsed > 5 and p.bytes_done and p.bytes_total > p.bytes_done:
                left = elapsed / p.bytes_done * (p.bytes_total - p.bytes_done)
                eta = f" · about {format_duration(left)} left"
            status.update(f"Reading {label}: {p.done:,} of {total:,}{eta} · {escape(name)}")

        yield update


def format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def when(timestamp: str) -> str:
    """A record timestamp in local time, e.g. `2026-11-03 14:05`."""
    try:
        return parse_timestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return timestamp


def pick_file(eridi: Eridi, ref: str) -> FileInfo:
    """Resolve a file argument; when several match, ask which one."""
    try:
        return eridi.resolve_file(ref)
    except AmbiguousFileError as exc:
        out = console()
        out.print(f"{escape(ref)!r} matches several files:")
        candidates = exc.candidates[:20]
        for n, f in enumerate(candidates, start=1):
            label = eridi.root_label(f.root_id) or f.root_id
            out.print(f"  {n}. {escape(f.path)}  [dim]{escape(label)}[/dim]")
        if len(exc.candidates) > len(candidates):
            out.print(f"  … and {len(exc.candidates) - len(candidates)} more")
        while True:
            try:
                choice: int = typer.prompt("Which one", type=int)
            except typer.Abort:
                raise exc from None
            if 1 <= choice <= len(candidates):
                return candidates[choice - 1]
            out.print(f"Choose a number from 1 to {len(candidates)}.")


def highlighted(hit: SearchHit) -> Text:
    text = Text()
    for part, bold in hit.segments():
        text.append(part.replace("\n", " "), style="bold yellow" if bold else "")
    return text


def describe_entry(eridi: Eridi, entry: Entry) -> str:
    who = entry.author.name + (" · AI" if entry.author.kind == "agent" else "")
    files = ", ".join(s.path for s in entry.subjects)
    parts = [when(entry.created_at), entry.kind, who]
    if entry.outcome:
        parts.append(f"outcome: {entry.outcome}")
    if files:
        parts.append(files)
    return " · ".join(parts)


def print_diff(diff: FileDiff) -> None:
    out = console()
    if not diff.available:
        out.print("[yellow]The text of one of these versions is no longer stored.[/yellow]")
        return
    if not diff.lines:
        out.print("No differences.")
        return
    if diff.summary:
        out.print(f"[bold]{escape(diff.summary)}[/bold]")
    for line in diff.lines:
        if line.op == "@":
            out.print("[dim]…[/dim]")
        elif line.op == "-":
            out.print(Text(f"- {line.text}", style="red"))
        elif line.op == "+":
            out.print(Text(f"+ {line.text}", style="green"))
        else:
            out.print(Text(f"  {line.text}", style="dim"))


def check_outcome(outcome: str | None) -> None:
    if outcome is not None and outcome not in OUTCOMES:
        err_console().print(f"[red]Error:[/red] {OUTCOME_HELP}")
        raise typer.Exit(2)


@app.callback()
def main() -> None:
    """Find your lab's files, see what changed, and record why."""


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(f"eridi {__version__}")


@app.command()
def init(
    name: Annotated[str | None, typer.Option(help="Your name, shown on what you write.")] = None,
    email: Annotated[str | None, typer.Option(help="Your email (optional).")] = None,
    record_folder: Annotated[
        Path | None,
        typer.Option(
            help="Where the record lives. Choose a folder in your synced cloud storage to "
            "share it with your other computers or your lab."
        ),
    ] = None,
) -> None:
    """Set up Eridi: your name, and where to keep the record."""
    with session() as eridi:
        current = eridi.config
        if name is None:
            name = typer.prompt("Your name", default=current.user.name or None)
        if email is None:
            email = typer.prompt(
                "Email (optional)", default=current.user.email or "", show_default=False
            )
        if record_folder is None:
            default = str(current.effective_record_folder)
            answer = typer.prompt("Record folder", default=default)
            record_folder = Path(answer) if answer != default or current.record_folder else None
        config = eridi.init(name, email or None, record_folder)
        out = console()
        out.print(f"Ready. Writing as [bold]{escape(config.user.name)}[/bold].")
        out.print(f"Record: {escape(str(config.effective_record_folder))}")
        if not eridi.list_roots():
            out.print("Next: eridi storage add <folder>")


@storage_app.command("add")
def storage_add(
    path: Annotated[Path, typer.Argument(help="A local or synced folder.")],
    label: Annotated[str | None, typer.Option(help='A name for it, e.g. "Lab OneDrive".')] = None,
    search: Annotated[bool, typer.Option(help="Include it in scans and search.")] = True,
    save: Annotated[
        bool, typer.Option(help="Allow saving files here (from a later version).")
    ] = False,
    mark: Annotated[
        bool,
        typer.Option(
            help="Write a small .eridi-root.json into the folder so your other computers "
            "recognize it. Nothing else is ever written there."
        ),
    ] = False,
) -> None:
    """Connect a folder to search."""
    with session() as eridi:
        root = eridi.add_root(path, label, mark_folder=mark, search=search, save=save)
        console().print(f"Added [bold]{escape(root.label)}[/bold] ({escape(root.location)}).")
        console().print("Next: eridi scan")


@storage_app.command("list")
def storage_list() -> None:
    """Show connected storage."""
    with session() as eridi:
        roots = eridi.list_roots()
        if not roots:
            console().print("No storage connected. Add a folder with: eridi storage add <folder>")
            return
        out = console()
        for r in roots:
            search = "search on" if r.search else "search off"
            save = "save on" if r.save else "save off"
            out.print(f"[bold]{escape(r.label)}[/bold]  {escape(r.location)}")
            out.print(f"  [dim]{search} · {save} · {r.id}[/dim]")


@storage_app.command("remove")
def storage_remove(
    storage: Annotated[str, typer.Argument(help="The storage's name or ID.")],
) -> None:
    """Disconnect storage. Its files and the record are not touched."""
    with session() as eridi:
        matches = [r for r in eridi.list_roots() if storage in (r.id, r.label)]
        if len(matches) != 1:
            raise EridiError(f"No single storage is called {storage!r}. See `eridi storage list`.")
        eridi.remove_root(matches[0].id)
        console().print(f"Removed {escape(matches[0].label)}.")


@app.command()
def scan() -> None:
    """Look for new and changed files in all storage with search on."""
    with session() as eridi:
        if not eridi.list_roots():
            raise EridiError("No storage connected. Add a folder with: eridi storage add <folder>")
        with scan_progress() as progress:
            summary = eridi.scan(progress=progress)
        out = console()
        if summary.rebuilt:
            out.print("First scan done; later scans only read what changed.")
        for report in summary.reports:
            label = escape(eridi.root_label(report.root_id) or report.root_id)
            if report.unavailable:
                out.print(f"[red]{label}: not available.[/red] {escape(report.unavailable)}")
                continue
            parts = [
                f"{len(report.new)} new",
                f"{len(report.changed)} changed",
                f"{report.unchanged} unchanged",
            ]
            if report.moved:
                parts.append(f"{len(report.moved)} moved")
            if report.missing:
                parts.append(f"{len(report.missing)} missing")
            if report.skipped:
                parts.append(f"{len(report.skipped)} skipped")
            out.print(f"[bold]{label}[/bold]: {', '.join(parts)}.")
            if report.seconds >= 10:
                workers = f" with {report.workers} workers" if report.workers > 1 else ""
                out.print(f"  Took {format_duration(report.seconds)}{workers}.")
            for path, reason in report.errors:
                out.print(f"  [yellow]Could not read[/yellow] {escape(path)}: {escape(reason)}")
            if report.slow:
                out.print("  Slowest files to read:")
                for path, seconds in report.slow[:5]:
                    out.print(f"    {format_duration(seconds)}  {escape(path)}")
        pending = len(eridi.pending_changes())
        if pending:
            noun = "change needs" if pending == 1 else "changes need"
            out.print(f"{pending} {noun} a why: eridi changes")


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help='Words to find. Use "quotes" for a phrase.')],
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
) -> None:
    """Search files and the record."""
    with session() as eridi:
        hits = eridi.search(query, limit)
        out = console()
        if not hits:
            out.print(f"Nothing found for {escape(query)!r}.")
            return
        for hit in hits:
            if hit.kind == "entry":
                who = f"{hit.author} · {when(hit.created_at or '')}"
                where = f" · {escape(hit.path)}" if hit.path else ""
                meta = f"{escape(who)}{where} · {hit.id}"
                out.print(f"[cyan]{hit.entry_kind}[/cyan] {escape(hit.title)}  [dim]{meta}[/dim]")
            else:
                source = f"{hit.root_label} · " if hit.root_label else ""
                meta = escape(source + (hit.path or ""))
                out.print(f"[green]file[/green] {escape(hit.title)}  [dim]{meta}[/dim]")
            snippet = highlighted(hit)
            if snippet.plain.strip():
                out.print(Text("    ") + snippet)


@app.command()
def changes() -> None:
    """List changed files that still need a why."""
    with session() as eridi:
        pending = eridi.pending_changes()
        out = console()
        if not pending:
            out.print("Nothing waiting for a why.")
            return
        for change in pending:
            where = f"{escape(change.root_label or change.root_id)} · {when(change.detected_at)}"
            out.print(f"[bold]{escape(change.path)}[/bold]  [dim]{where}[/dim]")
            out.print(f"  {escape(change.summary or '(text changed)')}")
        out.print('Explain one: eridi why <file> "<why>"')


@app.command()
def diff(
    file: Annotated[str, typer.Argument(help="A path, part of a file name, or a file ID.")],
) -> None:
    """Show what changed in a file since its previous version."""
    with session() as eridi:
        info = pick_file(eridi, file)
        console().print(f"[bold]{escape(info.path)}[/bold]")
        print_diff(eridi.diff(file_id(info)))


@app.command()
def why(
    file: Annotated[str, typer.Argument(help="A path, part of a file name, or a file ID.")],
    text: Annotated[
        str | None, typer.Argument(help="Why it changed. Leave out to be asked.")
    ] = None,
    outcome: Annotated[str | None, typer.Option(help=OUTCOME_HELP)] = None,
    tag: Annotated[list[str] | None, typer.Option(help="A tag; repeat for more.")] = None,
) -> None:
    """Record why a file changed."""
    check_outcome(outcome)
    with session() as eridi:
        if not eridi.initialized:
            raise NotInitializedError()
        info = pick_file(eridi, file)
        ref = file_id(info)
        pending = eridi.pending_change_for(info)
        if text is None:
            out = console()
            out.print(f"[bold]{escape(info.path)}[/bold]")
            if pending is None:
                out.print(
                    "[dim]No unexplained change; this note will be about the current version.[/dim]"
                )
            else:
                print_diff(eridi.diff(ref))
            text = typer.prompt("Why did it change")
            if outcome is None:
                answer = (
                    typer.prompt(
                        f"Outcome ({', '.join(OUTCOMES)}, or Enter to skip)",
                        default="",
                        show_default=False,
                    )
                    .strip()
                    .lower()
                )
                outcome = answer or None
                check_outcome(outcome)
        entry = eridi.add_note(text, file=ref, outcome=outcome, tags=tag or ())
        explained = " (explains the latest change)" if entry.change else ""
        console().print(f"Saved note {entry.id} about {escape(info.path)}{explained}.")


@app.command()
def note(
    text: Annotated[str, typer.Argument(help="What to record.")],
    file: Annotated[str | None, typer.Option(help="The file it's about.")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="A tag; repeat for more.")] = None,
    outcome: Annotated[str | None, typer.Option(help=OUTCOME_HELP)] = None,
    decision: Annotated[bool, typer.Option(help="Record it as a decision.")] = False,
) -> None:
    """Add a note to the record, optionally about a file."""
    check_outcome(outcome)
    with session() as eridi:
        ref = file_id(pick_file(eridi, file)) if file else None
        entry = eridi.add_note(
            text, file=ref, tags=tag or (), outcome=outcome,
            kind="decision" if decision else "note",
        )  # fmt: skip
        console().print(f"Saved {entry.kind} {entry.id}.")


@app.command()
def outcome(
    target: Annotated[str, typer.Argument(help="An entry ID, or a file.")],
    result: Annotated[str, typer.Argument(help=OUTCOME_HELP)],
    text: Annotated[str, typer.Option(help="Details (optional).")] = "",
) -> None:
    """Record whether something worked."""
    check_outcome(result)
    with session() as eridi:
        if len(target) == 26 and target.isalnum():
            entry = eridi.add_outcome(result, entry_id=target.upper(), text=text)
        else:
            ref = file_id(pick_file(eridi, target))
            entry = eridi.add_outcome(result, file=ref, text=text)
        console().print(f"Saved outcome {entry.id}: {result}.")


@app.command()
def dismiss(
    file: Annotated[
        str | None, typer.Argument(help="The changed file not worth explaining.")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Dismiss every waiting change.")] = False,
) -> None:
    """Stop asking why a file changed (this change, this computer)."""
    with session() as eridi:
        if all_:
            count = eridi.dismiss_all()
            console().print(f"Dismissed {count} change{'s' if count != 1 else ''}.")
            return
        if file is None:
            raise EridiError("Name a file, or use --all to dismiss every waiting change.")
        info = pick_file(eridi, file)
        pending = eridi.pending_change_for(info)
        if pending is None:
            raise EridiError(f"{info.path} has no change waiting for a why.")
        eridi.dismiss(pending)
        console().print(f"Dismissed the change to {escape(info.path)}.")


@app.command("history")
def history_command(
    file: Annotated[str, typer.Argument(help="A path, part of a file name, or a file ID.")],
) -> None:
    """Show a file's versions and everything recorded about it."""
    with session() as eridi:
        info = pick_file(eridi, file)
        hist = eridi.file_history(file_id(info))
        out = console()
        status = "" if info.status == "present" else f" [red]({info.status})[/red]"
        out.print(
            f"[bold]{escape(info.path)}[/bold]{status}  [dim]{escape(hist.root_label or '')}[/dim]"
        )
        out.print("[bold]Versions[/bold]")
        for v in hist.versions:
            marks = [
                m
                for m in (
                    "current" if v.is_current else "",
                    "" if v.available else "text not stored",
                )
                if m
            ]
            extra = f"  [dim]{', '.join(marks)}[/dim]" if marks else ""
            out.print(f"  {when(v.first_seen)}  {v.text_sha256[:8]}{extra}")
        for from_path, _to_path, at in hist.moves:
            out.print(f"  {when(at)}  moved from {escape(from_path)}")
        out.print("[bold]Record[/bold]")
        if not hist.entries:
            out.print("  Nothing recorded yet.")
        for item in hist.entries:
            label = "" if item.state == "current" else f" [yellow]({item.state})[/yellow]"
            out.print(
                f"  {escape(describe_entry(eridi, item.entry))}{label}  [dim]{item.entry.id}[/dim]"
            )
            if item.entry.change and item.entry.change.summary:
                out.print(f"    [dim]{escape(item.entry.change.summary)}[/dim]")
            if item.entry.text:
                out.print(f"    {escape(item.entry.text)}")


@app.command()
def recent(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
) -> None:
    """Show the latest entries in the record."""
    with session() as eridi:
        entries = eridi.recent(limit)
        out = console()
        if not entries:
            out.print('The record is empty. Add a note with: eridi note "<text>"')
            return
        for entry in entries:
            out.print(f"[dim]{escape(describe_entry(eridi, entry))} · {entry.id}[/dim]")
            if entry.text:
                out.print(f"  {escape(entry.text)}")


@index_app.command("rebuild")
def index_rebuild() -> None:
    """Rebuild the search index from storage, snapshots and the record."""
    with session() as eridi:
        with scan_progress() as progress:
            report = eridi.rebuild_index(progress)
        files = sum(len(r.new) for r in report.roots.values())
        entries = report.record.added if report.record else 0
        console().print(f"Rebuilt: {files} files, {entries} record entries.")
        for root_id, reason in report.unavailable.items():
            console().print(
                f"[yellow]{escape(eridi.root_label(root_id) or root_id)}[/yellow]: {escape(reason)}"
            )


def eridi_command() -> list[str]:
    """How to start this Eridi from another program: the `eridi` executable if it's on the
    PATH, else this Python with `-m eridi`."""
    found = shutil.which("eridi")
    return [str(Path(found).resolve())] if found else [sys.executable, "-m", "eridi"]


@mcp_app.callback()
def mcp_main(ctx: typer.Context) -> None:
    """Run the MCP server over stdio (an MCP client starts this for you)."""
    if ctx.invoked_subcommand is None:
        from eridi.mcp.server import run_stdio

        run_stdio()


@mcp_app.command("config")
def mcp_config() -> None:
    """Print ready-to-paste settings for Claude Desktop and Claude Code."""
    command = eridi_command()
    desktop = {"mcpServers": {"eridi": {"command": command[0], "args": [*command[1:], "mcp"]}}}
    out = console()
    out.print("[bold]Claude Desktop[/bold]: add this to claude_desktop_config.json")
    out.print(json.dumps(desktop, indent=2), markup=False)
    out.print("")
    out.print("[bold]Claude Code[/bold]: run")
    out.print(f"claude mcp add eridi -- {shlex.join([*command, 'mcp'])}", markup=False)
    out.print("")
    with session() as eridi:
        if eridi.config.mcp_allow_writes:
            out.print("Assistants can add notes (marked as AI-written).")
        else:
            out.print("Read-only. To let assistants add notes, set \\[mcp] allow_writes = true in")
            out.print(str(eridi.config_file), markup=False)


def local_http() -> urllib.request.OpenerDirector:
    """An HTTP client for this computer only: never sends local requests through a proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def running_server() -> int | None:
    """The port of an Eridi server already running on this computer, if any."""
    from eridi.api.app import server_file

    try:
        port = int(json.loads(server_file().read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        with local_http().open(f"http://127.0.0.1:{port}/api/status", timeout=2):
            pass
    except urllib.error.HTTPError:
        return port  # it answered (401 without a token): it's ours and running
    except OSError:
        return None
    return port


def session_link(port: int) -> str | None:
    from eridi.api.security import stored_token

    token = stored_token()
    if token is None:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session-link",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with local_http().open(request, timeout=5) as response:
            url: str = json.loads(response.read())["url"]
            return url
    except (OSError, ValueError, KeyError):
        return None


@app.command("open")
def open_command(
    browser: Annotated[bool, typer.Option(help="Open the browser.")] = True,
) -> None:
    """Open Eridi in your browser, starting it if it isn't running."""
    port = running_server()
    if port is not None:
        url = session_link(port)
        if url is None:
            raise_error(
                f"Eridi is already running (port {port}) but this terminal can't sign in to it. "
                "Stop the other Eridi, then run `eridi open` again."
            )
        if browser:
            webbrowser.open(url)
        console().print(f"Eridi is running at http://127.0.0.1:{port}")
        if not browser:
            console().print(f"Open: {url}", markup=False)
        return
    from eridi.api.app import serve

    with session() as eridi:
        serve(eridi, open_browser=browser, announce=lambda m: console().print(m, markup=False))


def raise_error(message: str) -> NoReturn:
    err_console().print(f"[red]Error:[/red] {escape(message)}")
    raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Check that everything Eridi needs is working."""
    with session() as eridi:
        report = eridi.doctor()
        out = console()
        symbols = {
            "ok": "[green]✓[/green]",
            "warning": "[yellow]![/yellow]",
            "error": "[red]✗[/red]",
        }
        for check in report.checks:
            out.print(f"{symbols[check.status]} {escape(check.name)}: {escape(check.detail)}")
        if not report.ok:
            raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
