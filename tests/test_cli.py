"""M5: the command line, against the sample lab."""

import re
from pathlib import Path

import pytest
from docx import Document
from typer.testing import CliRunner, Result

from conftest import SampleLab
from eridi import __version__
from eridi.cli.main import app

runner = CliRunner()
PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


def run(*args: str, input: str | None = None) -> Result:
    return runner.invoke(app, list(args), input=input)


def ok(*args: str, input: str | None = None) -> str:
    result = run(*args, input=input)
    assert result.exit_code == 0, result.output
    return result.output


def fails(*args: str, input: str | None = None) -> str:
    result = run(*args, input=input)
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    return result.output


@pytest.fixture
def lab(sample_lab: SampleLab, tmp_path: Path) -> SampleLab:
    ok("init", "--name", "A. Okafor", "--email", "a@example.edu", "--record-folder",
       str(tmp_path / "record"))  # fmt: skip
    ok("storage", "add", str(sample_lab.root), "--label", "Lab Drive")
    ok("scan")
    return sample_lab


def edit_protocol(sample_lab: SampleLab, text: str) -> None:
    doc = Document(str(sample_lab.protocol_docx))
    doc.paragraphs[2].text = text
    doc.save(str(sample_lab.protocol_docx))


def test_help_and_version() -> None:
    assert "Find your lab's files" in ok("--help")
    assert ok("version").strip() == f"eridi {__version__}"
    for command in ("init", "scan", "search", "changes", "diff", "why", "note", "history",
                    "recent", "doctor", "outcome", "dismiss"):  # fmt: skip
        assert "Usage" in ok(command, "--help")
    assert "Usage" in ok("storage", "add", "--help")
    assert "Usage" in ok("index", "rebuild", "--help")


def test_init_interactive(tmp_path: Path) -> None:
    out = ok("init", input=f"J. Lindqvist\nj@example.edu\n{tmp_path / 'rec'}\n")
    assert "Writing as J. Lindqvist" in out
    assert (tmp_path / "rec" / "eridi-record.json").is_file()
    assert "eridi storage add" in out


def test_init_rejects_empty_name() -> None:
    assert "name is needed" in fails("init", "--name", " ", "--email", "", "--record-folder", "x")


def test_storage_add_list_remove(sample_lab: SampleLab, tmp_path: Path) -> None:
    out = ok("storage", "add", str(sample_lab.root), "--label", "Lab Drive")
    assert "Added Lab Drive" in out
    assert not (sample_lab.root / ".eridi-root.json").exists()
    listing = ok("storage", "list")
    assert "Lab Drive" in listing and "on" in listing
    assert "already connected" in fails("storage", "add", str(sample_lab.root))
    assert "not a folder" in fails("storage", "add", str(tmp_path / "missing"))
    assert "Removed Lab Drive" in ok("storage", "remove", "Lab Drive")
    assert "No storage connected" in ok("storage", "list")
    assert "No single storage" in fails("storage", "remove", "Lab Drive")


def test_storage_add_mark_and_no_search(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    ok("storage", "add", str(folder), "--mark", "--no-search")
    assert (folder / ".eridi-root.json").is_file()
    assert "off" in ok("storage", "list")


def test_scan_without_storage() -> None:
    assert "eridi storage add" in fails("scan")


def test_scan_reports(lab: SampleLab) -> None:
    edit_protocol(lab, "Primary antibody incubation: 2 h at RT.")
    out = ok("scan")
    assert "Lab Drive: 0 new, 1 changed, 8 unchanged." in out
    assert "1 change needs a why" in out


def test_search(lab: SampleLab) -> None:
    out = ok("search", "blocking buffer")
    assert "Blocking buffer comparison" in out
    assert "Lab Drive · Papers/Blocking_buffer_comparison.pdf" in out
    assert "Nothing found" in ok("search", "zebrafish")
    ok("search", '"unbalanced')  # odd input never crashes


def test_changes_diff_why_history(lab: SampleLab) -> None:
    assert "Nothing waiting" in ok("changes")
    edit_protocol(lab, "Primary antibody incubation: 2 h at RT.")
    ok("scan")
    out = ok("changes")
    assert PROTOCOL in out
    assert "Primary antibody incubation: 1 h at RT → 2 h at RT" in out

    out = ok("diff", "blocking_v7")
    assert "- Primary antibody incubation: 1 h at RT." in out
    assert "+ Primary antibody incubation: 2 h at RT." in out

    out = ok("why", "blocking_v7", "The new lot needs longer incubation.", "--outcome", "worked")
    assert "explains the latest change" in out
    assert "Nothing waiting" in ok("changes")

    out = ok("history", PROTOCOL)
    assert "current" in out
    assert "The new lot needs longer incubation." in out
    assert "outcome: worked" in out
    assert "Primary antibody incubation: 1 h at RT → 2 h at RT" in out
    assert "The new lot needs longer incubation" in ok("search", "longer incubation")


def test_why_interactive_shows_the_diff_first(lab: SampleLab) -> None:
    edit_protocol(lab, "Primary antibody incubation: overnight at 4 °C.")
    ok("scan")
    out = ok("why", PROTOCOL, input="Overnight gives cleaner bands.\npartial\n")
    assert out.index("+ Primary antibody incubation: overnight") < out.index("Why did it change")
    assert "Saved note" in out
    assert "outcome: partial" in ok("history", PROTOCOL)


def test_why_interactive_rejects_bad_outcome(lab: SampleLab) -> None:
    assert "Did it work?" in fails("why", PROTOCOL, input="Because.\ngreat\n")


def test_why_needs_init(sample_lab: SampleLab) -> None:
    assert "eridi init" in fails("why", "anything", "text")


def test_note_needs_init() -> None:
    assert "eridi init" in fails("note", "hello")


def test_unknown_file(lab: SampleLab) -> None:
    out = fails("diff", "no_such_protocol")
    assert "No file matches 'no_such_protocol'" in out


def test_ambiguous_name_offers_a_pick_list(lab: SampleLab) -> None:
    out = ok("history", "Papers/", input="2\n")
    assert "1. Papers/Blocking_buffer_comparison.pdf" in out
    assert "2. Papers/Scanned_notebook_page.pdf" in out
    assert "Papers/Scanned_notebook_page.pdf" in out.split("Which one")[1]


def test_ambiguous_name_without_an_answer(lab: SampleLab) -> None:
    out = fails("history", "Papers/", input="")
    assert "matches 2 files" in out


def test_ambiguous_pick_reasks_out_of_range(lab: SampleLab) -> None:
    out = ok("diff", "Papers/", input="9\n1\n")
    assert "Choose a number from 1 to 2" in out


def test_note_outcome_recent_dismiss(lab: SampleLab) -> None:
    out = ok("note", "Switched all phospho blots to BSA.", "--file", PROTOCOL, "--tag", "blocking",
             "--decision")  # fmt: skip
    assert "Saved decision" in out
    entry_id = ULID.search(out)
    assert entry_id is not None
    assert "Saved outcome" in ok(
        "outcome", entry_id.group(), "worked", "--text", "Clean background"
    )
    assert "Saved outcome" in ok("outcome", "lab_meeting", "unknown")
    assert "Did it work?" in fails("outcome", entry_id.group(), "great")
    assert "no record entry" in fails("outcome", "01JC8Z9K3V5W6X7Y8Z9A0B1C2D", "worked")
    recent = ok("recent", "--limit", "5")
    assert "Switched all phospho blots to BSA." in recent
    assert "decision" in recent and "outcome: worked" in recent

    assert "no change waiting" in fails("dismiss", PROTOCOL)
    edit_protocol(lab, "Primary antibody incubation: 3 h at RT.")
    ok("scan")
    assert "Dismissed" in ok("dismiss", PROTOCOL)
    assert "Nothing waiting" in ok("changes")


def test_recent_empty(lab: SampleLab) -> None:
    assert "The record is empty" in ok("recent")


def test_index_rebuild(lab: SampleLab) -> None:
    ok("note", "kept through a rebuild")
    out = ok("index", "rebuild")
    assert "Rebuilt: 9 files, 1 record entries." in out
    assert "kept through a rebuild" in ok("search", "rebuild")


def test_doctor(lab: SampleLab) -> None:
    out = ok("doctor")
    assert "✓ Search engine" in out
    assert "Record folder" in out
    assert "scans without a text layer" in out


def test_doctor_fails_on_missing_storage(lab: SampleLab, tmp_path: Path) -> None:
    usb = tmp_path / "usb"
    usb.mkdir()
    ok("storage", "add", str(usb), "--label", "USB")
    usb.rmdir()
    out = fails("doctor")
    assert "✗ Storage 'USB'" in out


def test_broken_config_is_explained(isolated_home: Path) -> None:
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.toml").write_text("[user\nname = 1\n", encoding="utf-8")
    out = fails("storage", "list")
    assert "config.toml is not valid TOML" in out
    assert "line 1" in out
