"""Acceptance tests for the vision path: scanned invoices, end to end.

The scanned invoice has no text layer. The scripted vision model reads it under
the strict schema; the Run either verifies the extraction against LedgerLite or
escalates with the low-confidence reason. Text-layer invoices keep the text
path and are covered by the walking-skeleton tests.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from mocks.ledgerlite import db as ledgerlite
from tests.support import (
    SCAN_PATH,
    WORK_ORDER,
    ScriptedClient,
    run_settings,
    text_turn,
    tool_turn,
    vision_turn,
)
from verirun.cli import main
from verirun.engine.states import RunState
from verirun.runs.store import RunStore

FILED_INVOICE_ID = "INV-3003"

SCAN_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the Paperline scan and extract its fields",
            "allowed_tools": ["mail.read", "files.extract"],
            "done_criterion": "The invoice fields are extracted with confidence",
        },
        {
            "id": "step-2",
            "goal": "Validate and file the invoice in LedgerLite",
            "allowed_tools": [
                "erp.list_vendors",
                "erp.get_purchase_order",
                "erp.get_goods_receipt",
                "erp.file_invoice",
            ],
            "done_criterion": "PP-2026-042 is filed against PO-2008 and GR-2508",
        },
        {
            "id": "step-3",
            "goal": "Schedule the payment and archive the source",
            "allowed_tools": ["erp.schedule_payment", "files.archive"],
            "done_criterion": "The payment is scheduled and the source is in processed",
        },
    ]
}


def scanned_script(**confidences: float) -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(SCAN_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7008"}, call_id="c1"),
            tool_turn("files.extract", {"path": SCAN_PATH}, call_id="c2"),
            vision_turn(**confidences),
            text_turn("Extracted PP-2026-042 from the scan."),
            tool_turn("erp.list_vendors", {}, call_id="c3"),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2008"}, call_id="c4"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2508"}, call_id="c5"),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "PP-2026-042",
                    "vendor_id": "V-1008",
                    "amount": 1050.00,
                    "po_id": "PO-2008",
                    "gr_id": "GR-2508",
                },
                call_id="c6",
            ),
            text_turn(f"Filed as {FILED_INVOICE_ID}."),
            tool_turn(
                "erp.schedule_payment",
                {"invoice_id": FILED_INVOICE_ID, "scheduled_for": "2026-10-05"},
                call_id="c7",
            ),
            tool_turn(
                "files.archive",
                {"path": SCAN_PATH, "directory": "processed"},
                call_id="c8",
            ),
            text_turn("Payment scheduled and source archived."),
            # The Verifier re-reads the scan itself, independently of the Run.
            vision_turn(),
        ]
    )


def run_scanned(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state, capsys, **confidences: float
) -> tuple[int, str, RunStore, str]:
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=scanned_script(**confidences),
    )
    output = capsys.readouterr().out
    store = RunStore(settings.run_db)
    run_id = store.list_runs()[0].id
    return code, output, store, run_id


def test_a_scanned_invoice_is_extracted_above_threshold_and_verified(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys,
) -> None:
    code, output, store, run_id = run_scanned(tmp_path, ledgerlite_db, maildesk_state, capsys)
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)

    run = store.get_run(run_id)
    assert code == 0
    assert run.state is RunState.COMPLETED
    assert "[pass] invoice-filed" in output
    assert "[pass] source-archived" in output

    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'PP-2026-042' AND scenario = 'filed'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert invoice["vendor_id"] == "V-1008"
    assert invoice["amount_cents"] == 105_000
    assert [payment["status"] for payment in payments] == ["scheduled"]
    assert (maildesk_state.shared_root / "processed" / "PP-2026-042.pdf").is_file()

    evidence = json.loads(
        (settings.run_db.parent / run_id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["result"] == "verified"
    assert evidence["verified"] is True
    extraction = evidence["extractions"][0]
    assert extraction["path"] == SCAN_PATH
    assert extraction["method"] == "vision"
    assert extraction["ok"] is True
    assert extraction["fields"]["invoice_number"]["value"] == "PP-2026-042"
    assert extraction["fields"]["amount_cents"] == {"value": 105_000, "confidence": 0.95}
    assert extraction["min_confidence"] == 0.93
    check = next(
        item for item in evidence["verification"]["checks"] if item["id"] == "invoice-filed"
    )
    assert any(item.startswith("extraction:") for item in check["evidence"])

    assert run.steps_used == 8
    assert run.cost_usd > 0
    assert evidence["cost_usd"] == run.cost_usd


def test_a_low_confidence_scan_escalates_with_the_reason(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys,
) -> None:
    code, output, store, run_id = run_scanned(
        tmp_path, ledgerlite_db, maildesk_state, capsys, amount=0.31
    )
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)

    run = store.get_run(run_id)
    assert code == 1
    assert run.state is RunState.NEEDS_HUMAN
    assert "needs_human" in output
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert "low-confidence" in escalation.reason
    assert "amount" in escalation.reason
    assert "0.31" in escalation.reason
    assert "0.80" in escalation.reason
    assert escalation.context["threshold"] == 0.8
    assert escalation.context["low_confidence_fields"] == ["amount"]

    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        filed = conn.execute(
            "SELECT * FROM invoices WHERE number = 'PP-2026-042' AND scenario = 'filed'"
        ).fetchall()
    assert filed == []

    evidence = json.loads(
        (settings.run_db.parent / run_id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["result"] == "needs_human"
    assert evidence["verified"] is False
    extraction = evidence["extractions"][0]
    assert extraction["ok"] is False
    assert extraction["fields"]["amount_cents"]["confidence"] == 0.31
    assert any(
        "low-confidence" in item["reason"] for item in evidence["escalations"]
    )
