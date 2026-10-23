"""Packaging artifacts a stranger can open without running anything."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sample_evidence_pack_is_standalone() -> None:
    path = ROOT / "docs" / "sample-evidence" / "index.html"
    html = path.read_text(encoding="utf-8")

    assert '<body data-run-id="RUN-' in html
    assert 'data-verified="true"' in html
    assert "invoice-filed" in html
    assert "payment-scheduled" in html
    assert "source-archived" in html
    assert "<h2>Approvals</h2>" in html
    assert "spend-limits" in html
    assert "approved" in html
    assert not re.search(r'(?:src|href)="https?://', html)


def test_recording_artifacts_are_committed() -> None:
    gif = (ROOT / "docs" / "media" / "demo.gif").read_bytes()
    webm = (ROOT / "docs" / "media" / "demo.webm").read_bytes()

    assert gif[:6] in (b"GIF87a", b"GIF89a")
    assert webm[:4] == b"\x1aE\xdf\xa3"


def test_readme_presents_the_pitch_before_the_details() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.index("## Quickstart") < readme.index("## Architecture")
    assert readme.index("## Architecture") < readme.index("## Evaluation results")
    assert "docs/media/demo.gif" in readme
    assert "docs/sample-evidence/" in readme


def test_pages_workflow_publishes_the_docs_folder() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")

    assert "actions/deploy-pages" in workflow
    assert "path: docs" in workflow
