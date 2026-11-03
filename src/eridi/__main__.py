"""`python -m eridi` runs the command line."""

from eridi.cli.main import app

# The guard matters: scan workers re-import the main module, and must not start the CLI.
if __name__ == "__main__":
    app(prog_name="eridi")
