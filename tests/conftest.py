"""Shared fixtures.

`sample_lab` builds a realistic lab folder at test time, so no binary files are committed.
Every test runs with `ERIDI_HOME` pointed at a temporary folder, so tests never touch the
developer's real Eridi data.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "eridi-home"
    monkeypatch.setenv("ERIDI_HOME", str(home))
    yield home


def _pdf(page_streams: list[bytes]) -> bytes:
    """Build a minimal valid PDF. Each stream draws one page."""
    objects: list[bytes] = []
    n_pages = len(page_streams)
    first_page = 3
    font_obj = first_page + 2 * n_pages
    kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, stream in enumerate(page_streams):
        content_obj = first_page + 2 * i + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _text_stream(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


@dataclass(frozen=True)
class SampleLab:
    root: Path
    protocol_docx: Path
    lot_log_xlsx: Path
    text_pdf: Path
    scanned_pdf: Path
    notes_md: Path
    analysis_r: Path
    notebook_ipynb: Path
    gdoc_stub: Path
    office_temp: Path
    unicode_doc: Path


@pytest.fixture
def sample_lab(tmp_path: Path) -> SampleLab:
    root = tmp_path / "Lab Drive"
    (root / "Protocols").mkdir(parents=True)
    (root / "Inventory").mkdir()
    (root / "Papers").mkdir()
    (root / "Notes").mkdir()
    (root / "Analysis").mkdir()
    (root / "Protokolle_Ü").mkdir()

    protocol = root / "Protocols" / "Western_blot_blocking_v7.docx"
    doc = Document()
    doc.add_heading("Western blot: blocking and antibody incubation", level=1)
    doc.add_paragraph("Block membrane in 5% BSA in TBST for 1 h at room temperature.")
    doc.add_paragraph("Primary antibody incubation: 1 h at RT.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Reagent"
    table.cell(0, 1).text = "Dilution"
    table.cell(1, 0).text = "Anti-pERK (lot B2291)"
    table.cell(1, 1).text = "1:1000"
    doc.save(str(protocol))

    lot_log = root / "Inventory" / "Antibody_lot_log_2026.xlsx"
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Lots"
    ws.append(["Antibody", "Lot", "Received", "Notes"])
    ws.append(["Anti-pERK", "B2291", "2026-08-20", "Previous lot discontinued"])
    ws.append(["Anti-GAPDH", "G1150", "2026-06-02", "Loading control"])
    wb.save(str(lot_log))

    text_pdf = root / "Papers" / "Blocking_buffer_comparison.pdf"
    text_pdf.write_bytes(
        _pdf(
            [
                _text_stream(
                    [
                        "Blocking buffer comparison",
                        "Milk caused high background",
                        "with phospho-specific antibodies.",
                    ]
                )
            ]
        )
    )

    scanned_pdf = root / "Papers" / "Scanned_notebook_page.pdf"
    scanned_pdf.write_bytes(_pdf([b"0.9 g 100 100 400 600 re f"]))  # a grey box, no text

    notes = root / "Notes" / "lab_meeting_2026-10-14.md"
    notes.write_text(
        "# Lab meeting, 14 Oct 2026\n\n"
        "- Switch blocking buffer from milk to BSA for phospho blots.\n",
        encoding="utf-8",
    )

    analysis = root / "Analysis" / "densitometry_analysis.R"
    analysis.write_text(
        "# Band densitometry normalized to GAPDH\nratio <- perk / gapdh\n", encoding="utf-8"
    )

    notebook = root / "Analysis" / "qc_summary.ipynb"
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "markdown",
                        "metadata": {},
                        "source": ["# QC summary\n", "Signal check after lot change"],
                    },
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "execution_count": 1,
                        "outputs": [
                            {
                                "output_type": "stream",
                                "name": "stdout",
                                "text": ["OUTPUT SHOULD NOT BE INDEXED"],
                            }
                        ],
                        "source": ["import pandas as pd"],
                    },
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    gdoc = root / "Protocols" / "Cell culture SOP.gdoc"
    gdoc.write_text(
        json.dumps(
            {
                "url": "https://docs.google.com/document/d/EXAMPLE/edit",
                "doc_id": "EXAMPLE",
                "email": "lab@example.edu",
            }
        ),
        encoding="utf-8",
    )

    office_temp = root / "Protocols" / "~$stern_blot_blocking_v7.docx"
    office_temp.write_bytes(b"\x00temp-lock-file")

    unicode_doc = root / "Protokolle_Ü" / "Färbung ñ v2.md"
    unicode_doc.write_text("Färbeprotokoll für Immunfluoreszenz\n", encoding="utf-8")

    return SampleLab(
        root=root,
        protocol_docx=protocol,
        lot_log_xlsx=lot_log,
        text_pdf=text_pdf,
        scanned_pdf=scanned_pdf,
        notes_md=notes,
        analysis_r=analysis,
        notebook_ipynb=notebook,
        gdoc_stub=gdoc,
        office_temp=office_temp,
        unicode_doc=unicode_doc,
    )
