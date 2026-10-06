"""Tests for the static Evidence HTML report: runs/<run-id>/evidence.html.

The HTML is generated from ``evidence.json`` plus the screenshots in the Run
directory. It must open standalone: inline CSS, inline images, no network.
"""

from __future__ import annotations

import base64
from pathlib import Path

from tests.support import seed_completed_run
from verirun.engine.orchestrator import report_run
from verirun.runs.evidence import build_evidence, write_evidence
from verirun.runs.report import render_evidence_html, write_evidence_html
from verirun.runs.store import RunStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def seeded_store(tmp_path: Path) -> RunStore:
    store = RunStore(tmp_path / "runs" / "verirun.db")
    seed_completed_run(store)
    return store


def test_render_shows_work_order_plan_timeline_and_verification(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    evidence = build_evidence(store.get_run("RUN-0001"), store)

    html = render_evidence_html(evidence)

    assert '<body data-run-id="RUN-0001"' in html
    assert "Process the invoices in the AP mailbox" in html
    assert "File the September invoices from the AP mailbox" in html
    assert "Read the invoice email and attachment" in html
    assert "invoice-filed" in html
    assert "INV-3003 agrees with its source" in html
    assert "Archived the source invoice" in html
    assert "erp.file_invoice" in html
    assert 'id="metric-cost">$0.42' in html
    assert 'id="metric-steps">3' in html


def test_render_embeds_screenshots_as_data_uris(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    run_dir = tmp_path / "runs" / "RUN-0001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "screenshot-001.png").write_bytes(PNG)

    html = render_evidence_html(
        build_evidence(store.get_run("RUN-0001"), store), directory=run_dir
    )

    assert "screenshot-001.png" in html
    assert "data:image/png;base64," in html
    assert base64.b64encode(PNG).decode("ascii") in html


def test_render_shows_approvals_extractions_and_open_questions(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    evidence = build_evidence(store.get_run("RUN-0001"), store)
    evidence["approvals"] = [
        {
            "id": 1,
            "key": "step-3:erp.schedule_payment:abcd1234",
            "step_id": "step-3",
            "tool": "erp.schedule_payment",
            "action": "payment.schedule",
            "arguments": {"invoice_id": "INV-3003"},
            "policy": "spend-limits",
            "rule": "approval-threshold",
            "reason": "Any payment above 10,000.00 USD must be held at an approval gate.",
            "status": "rejected",
            "decision_reason": "Budget is frozen until Q4",
            "decided_at": "2026-10-03T09:30:00+00:00",
            "executed_at": None,
            "created_at": "2026-10-03T09:00:00+00:00",
        }
    ]
    evidence["extractions"] = [
        {
            "path": "documents/invoices/PP-2026-042.pdf",
            "method": "vision",
            "pages": 1,
            "threshold": 0.8,
            "min_confidence": 0.93,
            "fields": {
                "invoice_number": {"value": "PP-2026-042", "confidence": 0.99},
                "amount_cents": {"value": 105_000, "confidence": 0.95},
            },
            "ok": True,
            "summary": "Extracted fields from 'documents/invoices/PP-2026-042.pdf'",
        }
    ]
    evidence["open_questions"] = ["Should the duplicate AQ-2026-014 be ignored?"]

    html = render_evidence_html(evidence)

    assert "erp.schedule_payment" in html
    assert "rejected" in html
    assert "Budget is frozen until Q4" in html
    assert "spend-limits" in html
    assert "PP-2026-042" in html
    assert "0.95" in html
    assert "Should the duplicate AQ-2026-014 be ignored?" in html


def test_render_escapes_run_data(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    evidence = build_evidence(store.get_run("RUN-0001"), store)
    evidence["request"] = "<script>alert('x')</script>"

    html = render_evidence_html(evidence)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_write_evidence_html_lands_next_to_the_json(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    run = store.get_run("RUN-0001")
    json_path = write_evidence(run, store, tmp_path / "runs" / run.id)

    html_path = write_evidence_html(json_path)

    assert html_path == tmp_path / "runs" / run.id / "evidence.html"
    assert 'data-run-id="RUN-0001"' in html_path.read_text(encoding="utf-8")


def test_report_run_writes_both_evidence_files(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)

    json_path = report_run("RUN-0001", store, tmp_path / "runs")

    assert json_path.name == "evidence.json"
    assert json_path.is_file()
    html_path = json_path.parent / "evidence.html"
    assert html_path.is_file()
    assert "invoice-filed" in html_path.read_text(encoding="utf-8")
