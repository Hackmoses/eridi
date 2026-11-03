"""Plain text, data files, code and Jupyter notebooks (standard library only)."""

import json
from typing import Any

from eridi.core.extract.base import ExtractResult, decode_text, register

CODE_EXTENSIONS = (".py", ".r", ".m", ".jl", ".sh", ".sql")


def _markdown_title(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


@register(".txt", ".csv", ".tsv", ".json", *CODE_EXTENSIONS)
def extract_text(data: bytes) -> ExtractResult:
    return ExtractResult(decode_text(data))


@register(".md", ".markdown")
def extract_markdown(data: bytes) -> ExtractResult:
    text = decode_text(data)
    return ExtractResult(text, _markdown_title(text))


def _source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


@register(".ipynb")
def extract_notebook(data: bytes) -> ExtractResult:
    """Markdown and code cells. Outputs are left out: they are large and often generated."""
    notebook = json.loads(decode_text(data))
    parts: list[str] = []
    title = None
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") not in ("markdown", "code"):
            continue
        source = _source(cell).strip()
        if not source:
            continue
        if title is None and cell["cell_type"] == "markdown":
            title = _markdown_title(source)
        parts.append(source)
    return ExtractResult("\n\n".join(parts), title)
