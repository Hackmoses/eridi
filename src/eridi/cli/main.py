"""Command line entry point. Commands arrive in milestone M5."""

import typer

from eridi import __version__

app = typer.Typer(
    name="eridi",
    help="Find your lab's files, see what changed, and record why.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Find your lab's files, see what changed, and record why."""


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(f"eridi {__version__}")


if __name__ == "__main__":
    app()
