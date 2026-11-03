"""Entry point of the packaged engine (`eridi-engine`), built with PyInstaller.

`freeze_support()` must run first: scan workers are separate processes, and in a frozen
app each one starts by running this same executable.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from eridi.cli.main import app

    app(prog_name="eridi")
