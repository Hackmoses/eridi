"""M2.3: text extraction from the sample lab's files."""

import io
import json
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from conftest import SampleLab
from eridi.core.extract import extract, extract_file, extractor_for, registered_extensions
from eridi.core.extract.base import clean_text, decode_text
from eridi.core.extract.office import MAX_XLSX_CELLS


def test_docx_paragraphs_and_tables(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.protocol_docx.name, sample_lab.protocol_docx.read_bytes())
    assert result.status == "ok"
    assert result.title == "Western blot: blocking and antibody incubation"
    lines = result.text.splitlines()
    assert "Block membrane in 5% BSA in TBST for 1 h at room temperature." in lines
    assert "Primary antibody incubation: 1 h at RT." in lines
    assert "Reagent | Dilution" in lines
    assert "Anti-pERK (lot B2291) | 1:1000" in lines
    # Document order: the table comes after the paragraphs.
    assert lines.index("Reagent | Dilution") > lines.index(
        "Primary antibody incubation: 1 h at RT."
    )


def test_xlsx_sheets_and_cells(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.lot_log_xlsx.name, sample_lab.lot_log_xlsx.read_bytes())
    assert result.status == "ok"
    lines = result.text.splitlines()
    assert lines[0] == "Lots"
    assert "Anti-pERK | B2291 | 2026-08-20 | Previous lot discontinued" in lines
    assert "Anti-GAPDH" in result.text
    assert result.title == "Antibody_lot_log_2026"


def test_xlsx_cell_cap(tmp_path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for r in range(MAX_XLSX_CELLS // 10 + 50):
        ws.append([f"r{r}c{c}" for c in range(10)])
    buffer = io.BytesIO()
    wb.save(buffer)
    result = extract("big.xlsx", buffer.getvalue())
    assert result.status == "ok"
    assert result.reason is not None and "20,000" in result.reason
    assert "r0c0" in result.text
    assert f"r{MAX_XLSX_CELLS // 10 + 49}c0" not in result.text
    assert result.text.count("|") <= MAX_XLSX_CELLS


def test_pptx_slide_text() -> None:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    assert slide.shapes.title is not None
    slide.shapes.title.text = "Lot change results"
    body = slide.placeholders[1]
    body.text_frame.text = "Band intensity restored with 2 h incubation"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "Lot"
    table.cell(0, 1).text = "Signal"
    table.cell(1, 0).text = "B2291"
    table.cell(1, 1).text = "strong"
    slide.notes_slide.notes_text_frame.text = "Mention GAPDH normalization"
    buffer = io.BytesIO()
    deck.save(buffer)
    result = extract("Lab meeting.pptx", buffer.getvalue())
    assert result.status == "ok"
    assert result.title == "Lot change results"
    assert "Band intensity restored with 2 h incubation" in result.text
    assert "B2291 | strong" in result.text
    assert "Mention GAPDH normalization" in result.text


def test_text_pdf(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.text_pdf.name, sample_lab.text_pdf.read_bytes())
    assert result.status == "ok"
    assert "Milk caused high background" in result.text
    assert result.title == "Blocking buffer comparison"


def test_pdf_without_text_needs_ocr(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.scanned_pdf.name, sample_lab.scanned_pdf.read_bytes())
    assert result.status == "needs_ocr"
    assert result.text == ""


def test_markdown(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.notes_md.name, sample_lab.notes_md.read_bytes())
    assert result.status == "ok"
    assert result.title == "Lab meeting, 14 Oct 2026"
    assert "Switch blocking buffer from milk to BSA" in result.text


def test_unicode_markdown(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.unicode_doc.name, sample_lab.unicode_doc.read_bytes())
    assert result.text == "Färbeprotokoll für Immunfluoreszenz"
    assert result.title == "Färbung ñ v2"


def test_r_script(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.analysis_r.name, sample_lab.analysis_r.read_bytes())
    assert result.status == "ok"
    assert "Band densitometry normalized to GAPDH" in result.text


def test_notebook_cells_without_outputs(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.notebook_ipynb.name, sample_lab.notebook_ipynb.read_bytes())
    assert result.status == "ok"
    assert result.title == "QC summary"
    assert "Signal check after lot change" in result.text
    assert "import pandas as pd" in result.text
    assert "OUTPUT SHOULD NOT BE INDEXED" not in result.text


def test_gdoc_stub_is_a_cloud_document(sample_lab: SampleLab) -> None:
    result = extract(sample_lab.gdoc_stub.name, sample_lab.gdoc_stub.read_bytes())
    assert result.status == "cloud_document"
    assert result.text == ""
    assert result.title == "Cell culture SOP"


@pytest.mark.parametrize(
    ("name", "data", "phrase"),
    [
        ("data.csv", b"sample,od600\nA1,0.42\n", "A1,0.42"),
        ("cfg.json", json.dumps({"buffer": "TBST"}).encode(), "TBST"),
        ("run.py", b"print('qc')\n", "print('qc')"),
        ("fit.m", b"% MATLAB fit\n", "MATLAB fit"),
        ("model.jl", b"# Julia model\n", "Julia model"),
        ("job.sh", b"#!/bin/sh\necho run\n", "echo run"),
        ("q.sql", b"SELECT lot FROM antibodies;\n", "SELECT lot"),
        ("notes.txt", "café".encode("cp1252"), "café"),
        ("UPPER.TXT", b"Extension case is ignored", "Extension case"),
    ],
)
def test_plain_and_code(name: str, data: bytes, phrase: str) -> None:
    result = extract(name, data)
    assert result.status == "ok"
    assert phrase in result.text


def test_unknown_type_is_name_only() -> None:
    result = extract("gel_image.tif", b"II*\x00binary")
    assert result.status == "name_only"
    assert result.text == ""
    assert result.title == "gel_image"
    assert extractor_for("gel_image.tif") is None


def test_corrupted_docx_returns_error_instead_of_raising() -> None:
    result = extract("broken.docx", b"PK\x03\x04 this is not really a zip")
    assert result.status == "error"
    assert result.reason is not None and "Could not read" in result.reason
    assert result.title == "broken"


@pytest.mark.parametrize("name", ["bad.xlsx", "bad.pptx", "bad.pdf", "bad.ipynb"])
def test_corrupted_files_never_raise(name: str) -> None:
    assert extract(name, b"\x00\x01garbage").status == "error"


def test_size_limit_skips_large_file_without_reading(tmp_path: Path) -> None:
    big = tmp_path / "huge_protocol.docx"
    with big.open("wb") as handle:
        handle.truncate(50 * 1024 * 1024)  # sparse: 50 MB without using the disk
    reads: list[int] = []

    def read() -> bytes:
        reads.append(1)
        return big.read_bytes()

    result = extract_file(big.name, big.stat().st_size, read, 25 * 1024 * 1024)
    assert result.status == "too_large"
    assert result.reason == "Larger than the 25 MB limit."
    assert reads == []


def test_extract_file_reads_under_the_limit(sample_lab: SampleLab) -> None:
    path = sample_lab.notes_md
    result = extract_file(path.name, path.stat().st_size, path.read_bytes, 1024 * 1024)
    assert result.status == "ok"


def test_extract_file_read_failure() -> None:
    def fail() -> bytes:
        raise PermissionError("denied")

    assert extract_file("a.md", 3, fail, None).status == "error"


def test_extract_file_skips_reading_unknown_types() -> None:
    def fail() -> bytes:
        raise AssertionError("should not read")

    assert extract_file("scan.czi", 3, fail, None).status == "name_only"


def test_registry_covers_planned_types() -> None:
    expected = {".docx", ".xlsx", ".pptx", ".pdf", ".txt", ".md", ".csv", ".json", ".ipynb"}
    expected |= {".py", ".r", ".m", ".jl", ".sh", ".sql", ".gdoc", ".gsheet", ".gslides"}
    assert expected <= set(registered_extensions())


def test_text_helpers() -> None:
    assert clean_text("a  \r\nb\x00\r\n\n") == "a\nb"
    assert decode_text("﻿BOM".encode()) == "BOM"
    assert decode_text("utf16".encode("utf-16")) == "utf16"
