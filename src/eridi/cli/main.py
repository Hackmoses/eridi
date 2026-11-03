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
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from eridi import __version__
from eridi.connectors.base import ConnectorError
from eridi.connectors.gdrive import DriveInfo as GoogleDriveInfo
from eridi.connectors.gdrive import root_id_for as google_root_id_for
from eridi.connectors.msauth import ApprovalRequiredError
from eridi.connectors.msgraph import DriveInfo, root_id_for
from eridi.core.changes import Activity, FileDiff, FileStats
from eridi.core.config import ConfigError
from eridi.core.index.db import IndexUnavailableError
from eridi.core.index.search import SearchHit, group_label, parse_query
from eridi.core.models import DATA_LABELS, OUTCOMES, Entry, FileInfo, Project, parse_timestamp
from eridi.core.outbox import OutboxJob
from eridi.core.record.store import RecordFolderError
from eridi.core.reports import Period, days_period, month_period
from eridi.core.scan import ScanProgress, ScanReport
from eridi.core.service import (
    AmbiguousFileError,
    Eridi,
    EridiError,
    NotInitializedError,
    SaveApprovalNeededError,
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
project_app = typer.Typer(
    help="Organize files and notes by project, across storage.", no_args_is_help=True
)
mcp_app = typer.Typer(
    help="Let AI assistants (Claude and other MCP clients) use the record. "
    "With no subcommand, runs the MCP server over stdio.",
    invoke_without_command=True,
)
app.add_typer(storage_app, name="storage")
app.add_typer(index_app, name="index")
app.add_typer(project_app, name="project")
outbox_app = typer.Typer(
    help="Files saved through Eridi: waiting to upload, needing a decision, or uploaded. "
    "With no subcommand, lists them and uploads any that are due.",
    invoke_without_command=True,
)
app.add_typer(mcp_app, name="mcp")
app.add_typer(outbox_app, name="outbox")

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
    except (
        EridiError, ConfigError, RecordFolderError, IndexUnavailableError, ConnectorError
    ) as exc:  # fmt: skip
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


def ago(timestamp: str) -> str:
    """How long ago, in words: "just now", "5 min ago", "yesterday", "12 Oct"."""
    try:
        moment = parse_timestamp(timestamp)
    except ValueError:
        return timestamp
    seconds = (datetime.now(moment.tzinfo) - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 14 * 86400:
        return f"{int(seconds // 86400)} days ago"
    return moment.astimezone().strftime("%d %b %Y")


def history_line(stats: FileStats | None) -> str:
    """e.g. "edited 2 days ago · 3 versions · 2 notes · needs a why" (rich markup)."""
    if stats is None or not stats.interesting:
        return ""
    parts = []
    if stats.last_edited:
        parts.append(f"edited {ago(stats.last_edited)}")
    if stats.versions > 1:
        parts.append(f"{stats.versions} versions")
    if stats.notes:
        parts.append(f"{stats.notes} note{'s' if stats.notes != 1 else ''}")
    line = "[dim]" + " · ".join(parts) + "[/dim]"
    if stats.needs_why:
        line += " · [yellow]needs a why[/yellow]"
    return line


def highlighted(hit: SearchHit) -> Text:
    text = Text()
    for part, bold in hit.segments():
        text.append(part.replace("\n", " "), style="bold yellow" if bold else "")
    return text


def describe_entry(eridi: Eridi, entry: Entry) -> str:
    who = entry.author.name + (" · AI" if entry.author.kind == "agent" else "")
    files = ", ".join(s.path for s in entry.subjects)
    kind = f"project {entry.project.name!r}" if entry.project else entry.kind
    parts = [when(entry.created_at), kind, who]
    if entry.outcome:
        parts.append(f"outcome: {entry.outcome}")
    if files:
        parts.append(files)
    return " · ".join(parts)


def in_project(eridi: Eridi, entry: Entry) -> str:
    if entry.project_id is None:
        return ""
    names = {p.id: p.name for p in eridi.list_projects()}
    return f" in {escape(names.get(entry.project_id, entry.project_id))}"


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
    join: Annotated[
        Path | None,
        typer.Option(
            help="Join a record folder a labmate already set up (e.g. in a shared SharePoint "
            "or Google Drive folder)."
        ),
    ] = None,
) -> None:
    """Set up Eridi: your name, and where to keep the record."""
    if join is not None and record_folder is not None:
        err_console().print("[red]Error:[/red] Use --join or --record-folder, not both.")
        raise typer.Exit(2)
    with session() as eridi:
        current = eridi.config
        if name is None:
            name = typer.prompt("Your name", default=current.user.name or None)
        if email is None:
            email = typer.prompt(
                "Email (optional)", default=current.user.email or "", show_default=False
            )
        if join is not None:
            config = eridi.init(name, email or None, join, join=True)
        else:
            if record_folder is None:
                default = str(current.effective_record_folder)
                answer = typer.prompt("Record folder", default=default)
                changed = answer != default or current.record_folder
                record_folder = Path(answer) if changed else None
            config = eridi.init(name, email or None, record_folder)
        out = console()
        out.print(f"Ready. Writing as [bold]{escape(config.user.name)}[/bold].")
        out.print(f"Record: {escape(str(config.effective_record_folder))}")
        others = [m for m in eridi.record_members() if m[0] != config.user.name]
        if others:
            total = sum(m[1] for m in others)
            names = ", ".join(escape(m[0]) for m in others[:5])
            more = f" and {len(others) - 5} more" if len(others) > 5 else ""
            out.print(f"{total} entries from {names}{more}.")
        if not eridi.list_roots():
            out.print("Next: eridi storage add <folder>")


def ask_to_mark() -> bool:
    return typer.confirm(
        "Shared with your other computers or your lab? Mark it so they recognize it "
        "(writes .eridi-root.json)",
        default=False,
    )


@storage_app.command("add")
def storage_add(
    path: Annotated[Path, typer.Argument(help="A local or synced folder.")],
    label: Annotated[str | None, typer.Option(help='A name for it, e.g. "Lab OneDrive".')] = None,
    search: Annotated[bool, typer.Option(help="Include it in scans and search.")] = True,
    save: Annotated[
        bool, typer.Option(help="Allow saving files here (from a later version).")
    ] = False,
    mark: Annotated[
        bool | None,
        typer.Option(
            "--mark/--no-mark",
            help="Write a small .eridi-root.json into the folder so your other computers and "
            "labmates recognize it. Nothing else is ever written there. Asked if not given.",
        ),
    ] = None,
) -> None:
    """Connect a folder to search."""
    with session() as eridi:
        root = eridi.add_root(path, label, mark_folder=bool(mark), search=search, save=save)
        out = console()
        out.print(f"Added [bold]{escape(root.label)}[/bold] ({escape(root.location)}).")
        if eridi.root_marker(root.id) == root.id and mark is None:
            out.print("[dim]Recognized from its folder marker.[/dim]")
        elif mark is None and sys.stdin.isatty() and ask_to_mark():
            eridi.mark_root(root.id)
            out.print("Marked.")
        out.print("Next: eridi scan")


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
            where = {"msgraph": "Microsoft", "gdrive": "Google Drive"}.get(r.kind, r.location)
            out.print(f"[bold]{escape(r.label)}[/bold]  {escape(where)}")
            marked = " · marked" if eridi.root_marker(r.id) == r.id else ""
            approved = approved_text(r.approved_labels)
            out.print(f"  [dim]{search} · {save}{marked}{approved} · {r.id}[/dim]")


def label_text(label: str) -> str:
    return DATA_LABELS.get(label, label).lower()


def approved_text(labels: tuple[str, ...]) -> str:
    if not labels:
        return ""
    names = ", ".join(DATA_LABELS.get(x, x).removeprefix("Contains ") for x in labels)
    return f" · approved for {names}"


@storage_app.command("add-microsoft")
def storage_add_microsoft(
    device_code: Annotated[
        bool, typer.Option(help="Sign in with a code instead of opening a browser here.")
    ] = False,
    drive: Annotated[
        list[str] | None,
        typer.Option(help="Library to add, by name or number from the list; repeat for more."),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Add every library listed.")] = False,
) -> None:
    """Connect OneDrive or SharePoint document libraries (read-only)."""
    with session() as eridi:
        out = console()
        try:
            account, drives = eridi.microsoft_sign_in(
                device_code=device_code, show=lambda message: out.print(escape(message))
            )
        except ApprovalRequiredError as exc:
            out.print("[yellow]Your university needs to approve this app.[/yellow]")
            out.print(f"Send IT this link: {exc.consent_url}")
            out.print("and the explanation in docs/IT_APPROVAL.md (what Eridi reads, and why).")
            raise typer.Exit(1) from None
        connected = {r.id for r in eridi.list_roots()}
        out.print(f"Signed in as [bold]{escape(account.username)}[/bold].")
        for n, d in enumerate(drives, start=1):
            added = "  [dim](connected)[/dim]" if root_id_for(d.id) in connected else ""
            out.print(f"  {n}. {escape(d.label)}{added}")
        if all_:
            chosen = drives
        elif drive:
            chosen = [pick_drive(drives, ref) for ref in drive]
        elif sys.stdin.isatty():
            answer = typer.prompt("Add which (numbers separated by commas, or Enter for none)",
                                  default="", show_default=False)  # fmt: skip
            chosen = [pick_drive(drives, ref) for ref in answer.replace(",", " ").split()]
        else:
            out.print("Choose with --drive <name or number>, or --all.")
            return
        for d in chosen:
            if root_id_for(d.id) in connected:
                continue
            root = eridi.add_microsoft_root(account.id, d)
            out.print(f"Added [bold]{escape(root.label)}[/bold].")
        if chosen:
            out.print("Next: eridi scan")
        out.print("[dim]To add a SharePoint library, follow its site in SharePoint first.[/dim]")


@storage_app.command("add-google")
def storage_add_google(
    drive: Annotated[
        list[str] | None,
        typer.Option(help="Drive to add, by name or number from the list; repeat for more."),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Add every drive listed.")] = False,
) -> None:
    """Connect Google Drive: My Drive and shared drives (read-only)."""
    with session() as eridi:
        out = console()
        account, drives = eridi.google_sign_in(show=lambda message: out.print(escape(message)))
        connected = {r.id for r in eridi.list_roots()}
        out.print(f"Signed in as [bold]{escape(account.email)}[/bold].")
        for n, d in enumerate(drives, start=1):
            added = "  [dim](connected)[/dim]" if google_root_id_for(d.id) in connected else ""
            out.print(f"  {n}. {escape(d.label)}{added}")
        if all_:
            chosen = drives
        elif drive:
            chosen = [pick_drive(drives, ref) for ref in drive]
        elif sys.stdin.isatty():
            answer = typer.prompt("Add which (numbers separated by commas, or Enter for none)",
                                  default="", show_default=False)  # fmt: skip
            chosen = [pick_drive(drives, ref) for ref in answer.replace(",", " ").split()]
        else:
            out.print("Choose with --drive <name or number>, or --all.")
            return
        for d in chosen:
            if google_root_id_for(d.id) in connected:
                continue
            root = eridi.add_google_root(account.id, d)
            out.print(f"Added [bold]{escape(root.label)}[/bold].")
        if chosen:
            out.print("Next: eridi scan")


def pick_drive[D: (DriveInfo, GoogleDriveInfo)](drives: list[D], ref: str) -> D:
    ref = ref.strip()
    if ref.isdigit() and 1 <= int(ref) <= len(drives):
        return drives[int(ref) - 1]
    matches = [d for d in drives if ref in (d.id, d.label) or ref.lower() == d.label.lower()]
    if len(matches) != 1:
        raise EridiError(f"No single library is called {ref!r}. Use its number from the list.")
    return matches[0]


@storage_app.command("mark")
def storage_mark(
    storage: Annotated[str, typer.Argument(help="The storage's name or ID.")],
) -> None:
    """Let your other computers and labmates recognize this folder, even at another path.

    Writes .eridi-root.json into the folder. If a labmate already marked it, this computer
    uses their ID instead, so everyone's notes about its files line up."""
    with session() as eridi:
        before = storage_id(eridi, storage)
        root = eridi.mark_root(before)
        out = console()
        if root.id != before:
            out.print(f"{escape(root.label)} now uses the shared ID {root.id}. Next: eridi scan")
        else:
            out.print(f"{escape(root.label)} is marked ({root.id}).")


@storage_app.command("set")
def storage_set(
    storage: Annotated[str, typer.Argument(help="The storage's name or ID.")],
    label: Annotated[str | None, typer.Option(help="A new name for it.")] = None,
    search: Annotated[
        bool | None, typer.Option("--search/--no-search", help="Include it in scans and search.")
    ] = None,
    save: Annotated[
        bool | None,
        typer.Option("--save/--no-save", help="Allow saving files here through Eridi."),
    ] = None,
    approve: Annotated[
        list[str] | None,
        typer.Option(help="Approve it for a data label, e.g. patient-data. Repeat for more."),
    ] = None,
    unapprove: Annotated[
        list[str] | None, typer.Option(help="Withdraw approval for a data label.")
    ] = None,
    device_code: Annotated[
        bool, typer.Option(help="For Microsoft: approve saving with a code, not a browser here.")
    ] = False,
) -> None:
    """Change a storage's name, turn Search or Save on or off, or approve it for data labels."""
    with session() as eridi:
        root_id = storage_id(eridi, storage)
        labels = None
        if approve or unapprove:
            current = eridi.get_root(root_id).approved_labels
            dropped = {x.strip().lower() for x in unapprove or []}
            labels = [x for x in (*current, *(approve or [])) if x.strip().lower() not in dropped]
        try:
            root = eridi.update_root(
                root_id, label=label, search=search, save=save, approved_labels=labels
            )
        except SaveApprovalNeededError:
            out = console()
            out.print("Saving to Microsoft needs your permission for Eridi to write there.")
            out.print("[dim]Approve it in the Microsoft sign-in that opens now.[/dim]")
            eridi.allow_microsoft_saving(
                root_id, device_code=device_code, show=lambda m: out.print(escape(m))
            )
            root = eridi.update_root(
                root_id, label=label, search=search, save=save, approved_labels=labels
            )
        state = f"search {'on' if root.search else 'off'} · save {'on' if root.save else 'off'}"
        state += approved_text(root.approved_labels)
        console().print(f"[bold]{escape(root.label)}[/bold]  [dim]{state}[/dim]")


@storage_app.command("remove")
def storage_remove(
    storage: Annotated[str, typer.Argument(help="The storage's name or ID.")],
) -> None:
    """Disconnect storage. Its files and the record are not touched."""
    with session() as eridi:
        root_id = storage_id(eridi, storage)
        label = eridi.root_label(root_id)
        eridi.remove_root(root_id)
        console().print(f"Removed {escape(label or root_id)}.")


def folder_label(eridi: Eridi, root_id: str, path: str) -> str:
    label = eridi.root_label(root_id) or root_id
    return f"{label} / {path}" if path else f"{label} (all of it)"


def print_project(eridi: Eridi, project: Project) -> None:
    out = console()
    out.print(f"[bold]{escape(project.name)}[/bold]  [dim]{project.id}[/dim]")
    for f in project.folders:
        out.print(f"  {escape(folder_label(eridi, f.root_id, f.path))}")
    if not project.folders:
        out.print("  [dim]No folders yet: eridi project add-folder <project> <folder>[/dim]")
    if project.home_root_id:
        home = eridi.root_label(project.home_root_id) or project.home_root_id
        out.print(f"  [dim]Saves to {escape(home)}[/dim]")
    for label in project.labels:
        out.print(f"  [yellow]{escape(label_text(label).capitalize())}[/yellow]")


FOLDER_HELP = "A folder path, a storage name (all of it), or <storage ID>:<folder>."


@project_app.command("new")
def project_new(
    name: Annotated[str, typer.Argument(help='e.g. "Antibody validation".')],
    folder: Annotated[
        list[str] | None, typer.Option(help=FOLDER_HELP + " Repeat for more.")
    ] = None,
    home: Annotated[
        str | None, typer.Option(help="Storage name or ID that new files are saved to.")
    ] = None,
) -> None:
    """Start a project."""
    with session() as eridi:
        home_id = storage_id(eridi, home) if home else None
        project = eridi.create_project(name, home_root_id=home_id, folders=folder or ())
        print_project(eridi, project)


@project_app.command("list")
def project_list() -> None:
    """Show all projects."""
    with session() as eridi:
        projects = eridi.list_projects()
        if not projects:
            console().print('No projects yet. Start one with: eridi project new "<name>"')
            return
        for project in projects:
            print_project(eridi, project)


@project_app.command("add-folder")
def project_add_folder(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    folder: Annotated[str, typer.Argument(help=FOLDER_HELP)],
) -> None:
    """Add a folder; its files and notes about them join the project."""
    with session() as eridi:
        print_project(eridi, eridi.add_project_folder(project, folder))


@project_app.command("remove-folder")
def project_remove_folder(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    folder: Annotated[str, typer.Argument(help=FOLDER_HELP)],
) -> None:
    """Take a folder out of a project. Nothing in the folder is touched."""
    with session() as eridi:
        print_project(eridi, eridi.remove_project_folder(project, folder))


@project_app.command("rename")
def project_rename(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    name: Annotated[str, typer.Argument(help="The new name.")],
) -> None:
    """Rename a project (the old name stays in its history)."""
    with session() as eridi:
        print_project(eridi, eridi.rename_project(project, name))


@project_app.command("home")
def project_home(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    storage: Annotated[str, typer.Argument(help="Storage name or ID.")],
) -> None:
    """Set the storage new files for this project are saved to."""
    with session() as eridi:
        print_project(eridi, eridi.set_project_home(project, storage_id(eridi, storage)))


@project_app.command("label")
def project_label(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    label: Annotated[str, typer.Argument(help="A data label, e.g. patient-data.")],
    remove: Annotated[bool, typer.Option(help="Take the label off.")] = False,
) -> None:
    """Mark a project's data, e.g. "contains patient data". Its files can then be saved only
    to storage approved for that label. Labels are shared with the lab through the record."""
    with session() as eridi:
        current = eridi.get_project(project)
        wanted = label.strip().lower()
        labels = [x for x in current.labels if x != wanted] if remove else [*current.labels, wanted]
        print_project(eridi, eridi.set_project_labels(current.id, labels))


@project_app.command("show")
def project_show(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    limit: Annotated[int, typer.Option(min=1, max=500, help="Files and entries to show.")] = 20,
) -> None:
    """A project's files from all storage, changes needing a why, and its record."""
    with session() as eridi:
        overview = eridi.project_overview(project, limit)
        print_project(eridi, overview.project)
        out = console()
        out.print(f"[bold]Files[/bold] ({len(overview.files)})")
        for f in overview.files[:limit]:
            out.print(f"  {escape(f.path)}  [dim]{escape(eridi.root_label(f.root_id) or '')}[/dim]")
        if len(overview.files) > limit:
            out.print(f"  [dim]… and {len(overview.files) - limit} more[/dim]")
        if overview.pending:
            out.print(f"[bold]Needs a why[/bold] ({len(overview.pending)})")
            for change in overview.pending:
                summary = change.summary or "(text changed)"
                out.print(f"  {escape(change.path)}  [dim]{escape(summary)}[/dim]")
        out.print("[bold]Record[/bold]")
        for entry in overview.entries:
            out.print(f"  [dim]{escape(describe_entry(eridi, entry))} · {entry.id}[/dim]")
            if entry.text:
                out.print(f"    {escape(entry.text)}")


def storage_id(eridi: Eridi, ref: str) -> str:
    matches = [r for r in eridi.list_roots() if ref in (r.id, r.label)]
    if len(matches) != 1:
        raise EridiError(f"No single storage is called {ref!r}. See `eridi storage list`.")
    return matches[0].id


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
    project: Annotated[str | None, typer.Option(help="Only this project (name or ID).")] = None,
) -> None:
    """Search files and the record."""
    with session() as eridi:
        hits = eridi.search(query, limit, project)
        out = console()
        if not hits:
            out.print(f"Nothing found for {escape(query)!r}.")
            return
        grouped = len(parse_query(query)) > 1
        group = None
        for hit in hits:
            if grouped and group_label(hit) != group:
                group = group_label(hit)
                out.print(f"[bold]{escape(group)}[/bold]")
            if hit.kind == "entry":
                who = f"{hit.author} · {when(hit.created_at or '')}"
                where = f" · {escape(hit.path)}" if hit.path else ""
                meta = f"{escape(who)}{where} · {hit.id}"
                out.print(f"[cyan]{hit.entry_kind}[/cyan] {escape(hit.title)}  [dim]{meta}[/dim]")
            else:
                source = f"{hit.root_label} · " if hit.root_label else ""
                meta = escape(source + (hit.path or ""))
                out.print(f"[green]file[/green] {escape(hit.title)}  [dim]{meta}[/dim]")
                story = history_line(hit.stats)
                if story:
                    out.print(f"    {story}")
            snippet = highlighted(hit)
            if snippet.plain.strip():
                out.print(Text("    ") + snippet)


@app.command()
def changes(
    project: Annotated[str | None, typer.Option(help="Only this project (name or ID).")] = None,
) -> None:
    """List changed files that still need a why."""
    with session() as eridi:
        pending = eridi.pending_changes(project)
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
        console().print(
            f"Saved note {entry.id} about {escape(info.path)}{explained}{in_project(eridi, entry)}."
        )


@app.command()
def note(
    text: Annotated[str, typer.Argument(help="What to record.")],
    file: Annotated[str | None, typer.Option(help="The file it's about.")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="A tag; repeat for more.")] = None,
    outcome: Annotated[str | None, typer.Option(help=OUTCOME_HELP)] = None,
    decision: Annotated[bool, typer.Option(help="Record it as a decision.")] = False,
    project: Annotated[
        str | None,
        typer.Option(help="The project it belongs to. A note about a file joins its project."),
    ] = None,
) -> None:
    """Add a note to the record, optionally about a file."""
    check_outcome(outcome)
    with session() as eridi:
        ref = file_id(pick_file(eridi, file)) if file else None
        entry = eridi.add_note(
            text, file=ref, tags=tag or (), outcome=outcome,
            kind="decision" if decision else "note", project=project,
        )  # fmt: skip
        console().print(f"Saved {entry.kind} {entry.id}{in_project(eridi, entry)}.")


@app.command()
def outcome(
    target: Annotated[str, typer.Argument(help="An entry ID, or a file.")],
    result: Annotated[str, typer.Argument(help=OUTCOME_HELP)],
    text: Annotated[str, typer.Option(help="Details (optional).")] = "",
    version: Annotated[
        str | None,
        typer.Option(help="For a file: the version (from `eridi history`); default current."),
    ] = None,
) -> None:
    """Record whether something worked: for a note or decision, or a file's version."""
    check_outcome(result)
    with session() as eridi:
        if len(target) == 26 and target.isalnum():
            entry = eridi.add_outcome(result, entry_id=target.upper(), text=text)
        else:
            ref = file_id(pick_file(eridi, target))
            entry = eridi.add_outcome(result, file=ref, version=version, text=text)
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


def make_period(month: str | None, since: str | None) -> Period:
    try:
        return days_period(since) if since else month_period(month)
    except ValueError as exc:
        raise EridiError(str(exc)) from None


def result_text(result: str | None) -> str:
    color = {"worked": "green", "partial": "yellow", "failed": "red"}.get(result or "", "dim")
    return f" [{color}]{result}[/{color}]" if result else ""


@app.command()
def month(
    project: Annotated[str, typer.Argument(help="The project's name or ID.")],
    month_: Annotated[
        str | None, typer.Option("--month", help="Which month, YYYY-MM (default: this one).")
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Instead of a month: everything since YYYY-MM-DD.")
    ] = None,
) -> None:
    """What changed in a project this month: edits, new files, notes, and whether they worked."""
    with session() as eridi:
        report = eridi.project_period(project, make_period(month_, since))
        out = console()
        c = report.counts
        out.print(f"[bold]{escape(report.project.name)}[/bold] · {report.period.label}")
        out.print(
            f"[dim]{c['edited']} edited · {c['added']} added · {c['needs_why']} need a why · "
            f"{c['notes']} notes · {c['worked']} worked · {c['partial']} partly · "
            f"{c['failed']} failed[/dim]"
        )
        if report.changes:
            out.print("[bold]Files[/bold]")
        for a in report.changes:
            what = "Edited" if a.kind == "edited" else "Added"
            out.print(f"  {when(a.at)}  {what} {escape(a.path)}"
                      + ("  [yellow]needs a why[/yellow]" if a.needs_why else ""))  # fmt: skip
            if a.summary:
                out.print(f"    [dim]{escape(a.summary)}[/dim]")
            for e in a.explanations:
                out.print(f"    {escape(e.text)}{result_text(report.results.get(e.entry_id))}")
        if report.entries:
            out.print("[bold]Record[/bold]")
        for entry in report.entries:
            result = result_text(report.results.get(entry.id))
            out.print(f"  {escape(describe_entry(eridi, entry))}{result}  [dim]{entry.id}[/dim]")
            if entry.text:
                out.print(f"    {escape(entry.text)}")
        if not report.changes and not report.entries:
            out.print("Nothing changed in this period.")


@app.command()
def failed(
    file: Annotated[
        str | None, typer.Argument(help="A file (all its versions); default: everything.")
    ] = None,
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
) -> None:
    """Everything that failed: with one protocol, in a project, or everywhere."""
    with session() as eridi:
        ref = file_id(pick_file(eridi, file)) if file is not None else None
        found = eridi.failures(file=ref, project=project)
        out = console()
        if not found:
            out.print("Nothing recorded as failed.")
        for f in found:
            e, judged = f.entry, f.verdict.entry
            where = f"  {escape(f.path)}" if f.path else ""
            out.print(f"{when(judged.created_at)}{where}  [dim]{e.id}[/dim]")
            if e.text:
                out.print(f"  {escape(e.text)}")
            if judged.id != e.id and judged.text:
                out.print(
                    f"  [red]Failed:[/red] {escape(judged.text)} [dim]({judged.author.name})[/dim]"
                )
            elif judged.id != e.id or e.kind == "outcome":
                out.print(f"  [red]Failed[/red] [dim]({judged.author.name})[/dim]")


@app.command()
def questions(
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
    days: Annotated[
        int | None,
        typer.Option(min=0, help="Older than this many days (default: settings, 14)."),
    ] = None,
) -> None:
    """Open questions: changes and decisions with no outcome yet, and changes with no why."""
    with session() as eridi:
        found = eridi.open_questions(project=project, days=days)
        out = console()
        if not found:
            out.print("No open questions.")
        for q in found:
            age = ago(q.since)
            if q.kind == "no_why":
                assert q.change is not None  # noqa: S101 - set for no_why
                out.print(
                    f"{escape(q.change.path)}  [yellow]needs a why[/yellow]  [dim]{age}[/dim]"
                )
                if q.change.summary:
                    out.print(f"  [dim]{escape(q.change.summary)}[/dim]")
                continue
            assert q.entry is not None  # noqa: S101 - set for no_outcome
            where = escape(q.path) if q.path else escape(q.entry.kind.capitalize())
            out.print(f"{where}  [cyan]did it work?[/cyan]  [dim]{age} · {q.entry.id}[/dim]")
            out.print(f"  {escape(q.entry.text)}")
        if found:
            out.print(
                "[dim]Record a result: eridi outcome <entry ID or file> worked|partial|failed[/dim]"
            )


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
                    v.outcome or "",
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
        outcomes = eridi.outcomes()
        for item in hist.entries:
            label = "" if item.state == "current" else f" [yellow]({item.state})[/yellow]"
            result = (
                outcomes.outcome_of(item.entry) if item.entry.kind in ("note", "decision") else None
            )
            if result and result != item.entry.outcome:
                label += f" [dim]→ {result}[/dim]"
            out.print(
                f"  {escape(describe_entry(eridi, item.entry))}{label}  [dim]{item.entry.id}[/dim]"
            )
            if item.entry.change and item.entry.change.summary:
                out.print(f"    [dim]{escape(item.entry.change.summary)}[/dim]")
            if item.entry.text:
                out.print(f"    {escape(item.entry.text)}")


def print_activity(items: list[Activity]) -> None:
    out = console()
    day = None
    for a in items:
        moment = parse_timestamp(a.at).astimezone()
        label = moment.strftime("%A %d %B")
        if label != day:
            day = label
            out.print(f"[bold]{label}[/bold]")
        verb = "edited" if a.kind == "edited" else "added"
        out.print(f"  {moment:%H:%M}  {verb:<6}  {escape(a.path)}")
        if a.summary:
            out.print(f"           [dim]{escape(a.summary)}[/dim]")
        for note_ in a.explanations:
            out.print(f"           why: {escape(note_.text)}  [dim]{escape(note_.author)}[/dim]")
        if a.needs_why:
            out.print(f"           [yellow]needs a why[/yellow]: eridi why {escape(a.path)!r}")


@app.command()
def activity(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 30,
) -> None:
    """Show what you edited or added recently, newest first."""
    with session() as eridi:
        items = eridi.activity(limit)
        if not items:
            console().print("No edits yet. Run `eridi scan` (or `eridi watch`) after saving files.")
            return
        print_activity(items)


@app.command()
def watch() -> None:
    """Watch your storage and record edits seconds after you save (Ctrl+C to stop)."""
    from eridi.core.watch import Watcher

    with session() as eridi:
        if not eridi.list_roots():
            raise EridiError("No storage connected. Add a folder with: eridi storage add <folder>")
        out = console()

        def report(root_id: str, scanned: ScanReport) -> None:
            label = eridi.root_label(root_id) or root_id
            stamp = datetime.now().strftime("%H:%M:%S")
            for path in scanned.changed:
                out.print(f"{stamp}  [bold]edited[/bold]  {escape(label)} · {escape(path)}")
            for path in scanned.new:
                out.print(f"{stamp}  added   {escape(label)} · {escape(path)}")
            for old, new in scanned.moved:
                out.print(f"{stamp}  moved   {escape(old)} → {escape(new)}")
            for path in scanned.missing:
                out.print(f"{stamp}  removed {escape(path)}")

        def arrived(entries: list[Entry]) -> None:
            stamp = datetime.now().strftime("%H:%M:%S")
            for entry in entries:
                about = f" · {escape(entry.subjects[0].path)}" if entry.subjects else ""
                words = entry.text if len(entry.text) <= 80 else entry.text[:79] + "…"
                who = escape(entry.author.name)
                out.print(f"{stamp}  [cyan]{entry.kind}[/cyan]  {who}{about}: {escape(words)}")

        watcher = Watcher(eridi, on_report=report, on_entries=arrived)
        watcher.start()
        names = ", ".join(r.label for r in eridi.list_roots() if r.search)
        out.print(
            f"Watching {escape(names)} and the record. Save a file to see it here. Ctrl+C to stop."
        )
        for key, reason in watcher.polled.items():
            what = "the record folder" if key == "record" else eridi.root_label(key) or key
            out.print(
                f"[yellow]Can't watch {escape(what)}[/yellow] ({escape(reason)}); "
                "checking it every minute instead."
            )
        try:
            while True:
                time.sleep(1)
        finally:
            watcher.stop()


@app.command()
def recent(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
    project: Annotated[str | None, typer.Option(help="Only this project (name or ID).")] = None,
) -> None:
    """Show the latest entries in the record."""
    with session() as eridi:
        entries = eridi.recent(limit, project=project)
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


SAVE_STATES = {
    "saved": "saved on this computer",
    "queued": "waiting to upload",
    "uploading": "uploading",
    "uploaded": "uploaded",
    "needs_confirmation": "name taken: replace or save as new name",
    "blocked": "not allowed",
}


def print_save(eridi: Eridi, job: OutboxJob) -> None:
    out = console()
    where = eridi.root_label(job.root_id) or job.root_id
    path = job.uploaded_path or job.path
    color = {"uploaded": "green", "needs_confirmation": "yellow", "blocked": "red"}.get(
        job.state, "cyan"
    )
    out.print(
        f"[{color}]{SAVE_STATES.get(job.state, job.state)}[/{color}]  "
        f"{escape(where)} / {escape(path)}  [dim]{job.id}[/dim]"
    )
    if job.last_error and not job.done:
        out.print(f"  [dim]{escape(job.last_error)}[/dim]")
    if job.state == "needs_confirmation":
        out.print(f"  [dim]eridi outbox retry {job.id} --replace | --new-name[/dim]")


def on_exists_choice(replace_: bool, new_name: bool) -> str | None:
    if replace_ and new_name:
        raise EridiError("Choose --replace or --new-name, not both.")
    return "replace" if replace_ else "rename" if new_name else None


@app.command()
def save(
    file: Annotated[Path, typer.Argument(help="The file on this computer to save.")],
    project: Annotated[
        str | None, typer.Option(help="Save into this project's home storage.")
    ] = None,
    storage: Annotated[str | None, typer.Option(help="Save into this storage instead.")] = None,
    folder: Annotated[
        str | None,
        typer.Option(help="Folder inside the storage (default: the project's folder there)."),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--as", help="Save under this name (default: the same).")
    ] = None,
    replace_: Annotated[
        bool, typer.Option("--replace", help="If the name is taken, save as a new version.")
    ] = False,
    new_name: Annotated[
        bool, typer.Option("--new-name", help='If the name is taken, add " (2)" to it.')
    ] = False,
) -> None:
    """Save a copy of a file into a project's storage. Kept here until the upload works."""
    with session() as eridi:
        try:
            data = file.expanduser().read_bytes()
        except OSError as exc:
            raise EridiError(f"Could not read {file}: {exc.strerror or exc}") from None
        root_ref = storage_id(eridi, storage) if storage is not None else None
        job = eridi.save_file(
            data, name or file.name, project=project, storage=root_ref, folder=folder,
            on_exists=on_exists_choice(replace_, new_name) or "ask",
        )  # fmt: skip
        print_save(eridi, job)
        if job.waiting:
            console().print("[dim]It's safe on this computer; Eridi keeps trying while the "
                            "app is open, or run `eridi outbox`.[/dim]")  # fmt: skip


@outbox_app.callback()
def outbox_main(ctx: typer.Context) -> None:
    """List saved files, and upload any that are due."""
    if ctx.invoked_subcommand is not None:
        return
    with session() as eridi:
        eridi.process_outbox()
        jobs = eridi.outbox_jobs()
        if not jobs:
            console().print("Nothing saved through Eridi yet. Save a file with: eridi save")
        for job in reversed(jobs):
            print_save(eridi, job)


@outbox_app.command("retry")
def outbox_retry(
    job_id: Annotated[str, typer.Argument(help="The saved file's ID (from `eridi outbox`).")],
    replace_: Annotated[
        bool, typer.Option("--replace", help="Replace the existing file (as a new version).")
    ] = False,
    new_name: Annotated[
        bool, typer.Option("--new-name", help='Save as "name (2).ext" instead.')
    ] = False,
) -> None:
    """Try an upload again now, or answer "the name is taken"."""
    with session() as eridi:
        print_save(eridi, eridi.retry_save(job_id, on_exists_choice(replace_, new_name)))


@outbox_app.command("cancel")
def outbox_cancel(
    job_id: Annotated[str, typer.Argument(help="The saved file's ID (from `eridi outbox`).")],
) -> None:
    """Stop uploading a file and delete Eridi's copy. Your original is not touched."""
    with session() as eridi:
        job = eridi.cancel_save(job_id)
        console().print(f"Cancelled {escape(job.path)}.")


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
