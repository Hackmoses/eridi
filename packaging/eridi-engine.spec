# PyInstaller spec for the engine: one executable with the CLI, the local web app and the
# MCP server. Build with `uv run python scripts/build_engine.py` (it names the output the
# way the desktop app expects: eridi-engine-<target triple>[.exe]).
# ruff: noqa
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

hiddenimports = [
    *collect_submodules("eridi"),  # extractors and connectors register themselves on import
    *collect_submodules("uvicorn"),
    *collect_submodules("keyring.backends"),
    *collect_submodules("pypdfium2"),
    *collect_submodules("pypdfium2_raw"),
]
datas = [
    *collect_data_files("eridi"),  # the web UI
    *collect_data_files("pypdfium2_raw"),
    *collect_data_files("certifi"),
    *copy_metadata("eridi"),  # __version__
    *copy_metadata("keyring"),  # keychain backends are found through entry points
    *copy_metadata("mcp"),
]
binaries = [*collect_dynamic_libs("pypdfium2_raw")]

a = Analysis(
    ["engine_main.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "playwright", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="eridi-engine",
    console=True,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    codesign_identity=None,
    entitlements_file=None,
)
