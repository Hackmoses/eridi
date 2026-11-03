"""Checks that the sample lab fixture is realistic enough for later milestones."""

import json
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from conftest import SampleLab


def test_isolated_home(isolated_home: Path) -> None:
    import os

    assert os.environ["ERIDI_HOME"] == str(isolated_home)


def test_sample_lab_files_exist(sample_lab: SampleLab) -> None:
    for name in SampleLab.__dataclass_fields__:
        if name == "root":
            continue
        path: Path = getattr(sample_lab, name)
        assert path.is_file(), name
        assert path.is_relative_to(sample_lab.root)


def test_docx_is_readable(sample_lab: SampleLab) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Primary antibody incubation: 1 h at RT." in text
    assert doc.tables[0].cell(1, 0).text == "Anti-pERK (lot B2291)"


def test_xlsx_is_readable(sample_lab: SampleLab) -> None:
    ws = load_workbook(sample_lab.lot_log_xlsx).active
    assert ws is not None
    assert ws["B2"].value == "B2291"


def test_pdfs_are_well_formed(sample_lab: SampleLab) -> None:
    for pdf in (sample_lab.text_pdf, sample_lab.scanned_pdf):
        data = pdf.read_bytes()
        assert data.startswith(b"%PDF-1.4")
        assert data.rstrip().endswith(b"%%EOF")
    assert b"Blocking buffer comparison" in sample_lab.text_pdf.read_bytes()
    assert b"Tj" not in sample_lab.scanned_pdf.read_bytes()


def test_notebook_and_stub_are_json(sample_lab: SampleLab) -> None:
    assert json.loads(sample_lab.notebook_ipynb.read_text(encoding="utf-8"))["nbformat"] == 4
    assert "doc_id" in json.loads(sample_lab.gdoc_stub.read_text(encoding="utf-8"))


def test_unicode_folder(sample_lab: SampleLab) -> None:
    assert sample_lab.unicode_doc.parent.name == "Protokolle_Ü"
