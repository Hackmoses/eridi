"""M7: one browser smoke test of the web UI (Linux CI only).

Loads the UI through the one-time link, searches, and explains a change.
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document

from conftest import SampleLab
from eridi.core.service import Eridi
from live_server import live_server

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(sys.platform != "linux", reason="the browser test runs on Linux CI only"),
]
PROTOCOL = "Protocols/Western_blot_blocking_v7.docx"
PREINSTALLED = Path("/opt/pw-browsers/chromium")


@pytest.fixture
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        executable = os.environ.get("ERIDI_TEST_CHROMIUM") or (
            str(PREINSTALLED) if PREINSTALLED.exists() else None
        )
        try:
            b = p.chromium.launch(executable_path=executable)
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"No Chromium for Playwright here ({type(exc).__name__}).")
        yield b
        b.close()


def test_search_and_explain_a_change(browser: Any, sample_lab: SampleLab, tmp_path: Path) -> None:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        doc = Document(str(sample_lab.protocol_docx))
        doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
        doc.save(str(sample_lab.protocol_docx))
        eridi.scan()

        with live_server(eridi) as (port, guard):
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/?t={guard.new_link()}")

            # Search
            page.get_by_role("searchbox").fill("blocking buffer")
            page.wait_for_selector(".result")
            assert "Blocking buffer comparison" in page.inner_text("#results")
            assert page.locator(".result mark").count() > 0

            # Changes: the edited protocol is waiting for a why.
            page.get_by_role("link", name="Changes").click()
            page.wait_for_selector(".card")
            assert "1 h at RT → 2 h at RT" in page.inner_text(".changes")
            assert page.inner_text("#pending-count") == "1"
            page.get_by_role("button", name="Diff").click()
            page.wait_for_selector(".diff .add")
            page.get_by_label("Why").fill("The new antibody lot needs a longer incubation.")
            page.locator("#outcome-0").select_option("worked")
            page.get_by_role("button", name="Save").click()
            page.wait_for_selector(".saved")

            # History shows the note; search finds it.
            page.get_by_role("link", name="Search").click()
            page.get_by_role("searchbox").fill("longer incubation")
            page.wait_for_selector(".result.entry")
            page.get_by_role("link", name="history").first.click()
            page.wait_for_selector(".timeline")
            timeline = page.inner_text(".timeline")
            assert "The new antibody lot needs a longer incubation." in timeline
            assert "Current version" in timeline
            assert errors == []
        assert eridi.pending_changes() == []


def test_activity_updates_live_after_a_save(
    browser: Any, sample_lab: SampleLab, tmp_path: Path
) -> None:
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        with live_server(eridi, watch=True) as (port, guard):
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/?t={guard.new_link()}")
            page.get_by_role("link", name="Activity").click()
            page.wait_for_selector("text=No edits yet")
            doc = Document(str(sample_lab.protocol_docx))
            doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
            doc.save(str(sample_lab.protocol_docx))
            # No clicks: the watcher records the save and the page refreshes itself.
            page.wait_for_selector(".event", timeout=20_000)
            text = page.inner_text(".activity")
            assert "Edited" in text and "1 h at RT → 2 h at RT" in text
            assert "Needs a why" in text
            page.get_by_role("button", name="Diff").click()
            page.wait_for_selector(".diff .add")


def test_project_sidebar_and_page(browser: Any, sample_lab: SampleLab, tmp_path: Path) -> None:
    """M8.2: create a project in the sidebar, add a folder, see its files, search inside it."""
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        with live_server(eridi) as (port, guard):
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/?t={guard.new_link()}")
            page.get_by_placeholder("New project").fill("Western blots")
            page.get_by_role("button", name="Add", exact=True).click()
            page.wait_for_selector("h1:has-text('Western blots')")
            page.wait_for_selector("#project-list a[aria-current='page']:has-text('Western blots')")
            page.get_by_label("Folder to add").fill(str(sample_lab.root / "Protocols"))
            page.get_by_role("button", name="Add folder").click()
            page.wait_for_selector(f".filelist >> text={PROTOCOL}")
            assert "Lab Drive / Protocols" in page.inner_text(".folders")
            assert "lab_meeting" not in page.inner_text(".filelist")

            page.get_by_label("Search this project").fill("antibody")
            page.get_by_label("Search this project").press("Enter")
            page.wait_for_selector(".result")
            assert "Western_blot_blocking_v7" in page.inner_text(".results")
            assert "Antibody_lot_log" not in page.inner_text(".results")

            page.get_by_label("Add a note").fill("Plan: validate the new lot first.")
            page.get_by_role("button", name="Save").click()
            page.wait_for_selector(".timeline >> text=Plan: validate the new lot first.")
            assert errors == []


def test_save_a_file_into_a_project(browser: Any, sample_lab: SampleLab, tmp_path: Path) -> None:
    """M11: save a file from the project page; a patient-data label refuses unapproved storage."""
    upload = tmp_path / "gel_2026-11-03.txt"
    upload.write_text("Lane 3: strong band at 42 kDa.\n", encoding="utf-8")
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive", save=True)
        eridi.create_project("Western blots", home_root_id=root.id,
                             folders=[f"{root.id}:Protocols"])  # fmt: skip
        eridi.scan()
        with live_server(eridi) as (port, guard):
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/?t={guard.new_link()}")
            page.get_by_role("link", name="Western blots").click()
            page.wait_for_selector("h1:has-text('Western blots')")
            page.get_by_label("Files to save").set_input_files(str(upload))
            page.get_by_role("button", name="Save to Lab Drive").click()
            page.wait_for_selector(".saves .uploaded", timeout=20_000)
            assert "Protocols/gel_2026-11-03.txt" in page.inner_text(".saves")
            assert (sample_lab.root / "Protocols" / upload.name).exists()

            page.get_by_label("Contains patient data").check()
            page.wait_for_selector(".switch.flag")
            page.get_by_label("Files to save").set_input_files(str(upload))
            page.get_by_role("button", name="Save to Lab Drive").click()
            page.wait_for_selector("text=isn't approved")
            assert errors == []


def test_quick_outcome_buttons(browser: Any, sample_lab: SampleLab, tmp_path: Path) -> None:
    """M13.1: "Worked" on a note in a file's history records an outcome."""
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.scan()
        note = eridi.add_note("Blocking for 1 h is enough.", file=PROTOCOL)
        with live_server(eridi) as (port, guard):
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(
                f"http://127.0.0.1:{port}/?t={guard.new_link()}#/history?file={root.id}:{PROTOCOL}"
            )
            page.wait_for_selector(".timeline")
            page.locator(".entry").get_by_role("button", name="Worked").click()
            page.wait_for_selector(".entry .outcome-worked")
            assert errors == []
        assert eridi.outcomes().outcome_of(note) == "worked"


def test_project_views(browser: Any, sample_lab: SampleLab, tmp_path: Path) -> None:
    """M13.2: This month, Failed and Open questions on a project page."""
    with Eridi() as eridi:
        eridi.init("A. Okafor", None, tmp_path / "record")
        root = eridi.add_root(sample_lab.root, "Lab Drive")
        eridi.create_project("Blots", folders=[f"{root.id}:Protocols"])
        eridi.scan()
        doc = Document(str(sample_lab.protocol_docx))
        doc.paragraphs[2].text = "Primary antibody incubation: 2 h at RT."
        doc.save(str(sample_lab.protocol_docx))
        eridi.scan()
        note = eridi.add_note("New lot needs longer.", file=PROTOCOL)
        eridi.add_outcome("failed", entry_id=note.id)
        with live_server(eridi) as (port, guard):
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/?t={guard.new_link()}")
            page.get_by_role("link", name="Blots").click()
            page.get_by_role("button", name="This month").click()
            page.wait_for_selector(".view-panel >> text=1 edited")
            assert "New lot needs longer." in page.inner_text(".view-panel")
            page.locator(".tabs").get_by_role("button", name="Failed").click()
            page.wait_for_selector(".view-panel .outcome-failed")
            page.get_by_role("button", name="Open questions").click()
            page.wait_for_selector(".view-panel >> text=No open questions")
            assert errors == []
