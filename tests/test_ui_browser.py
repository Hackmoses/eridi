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
