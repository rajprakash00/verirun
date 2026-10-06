"""Acceptance tests for failure adaptation and escalation.

Each scenario is driven end to end against the real Mock Suite, with a scripted
LLM so the runs are deterministic and offline. The failures come from the real
seeded mock state: the transient write flag, the paid duplicate, the amount
mismatch, the missing purchase order, and the blocked vendor.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from mocks.ledgerlite import db as ledgerlite
from tests.support import ROOT, WORK_ORDER, ScriptedClient, text_turn, tool_turn
from verirun.cli import main
from verirun.config import Settings
from verirun.engine.adapt import FailureAction, classify_failure
from verirun.engine.models import Observation
from verirun.engine.states import RunState
from verirun.runs.store import RunStore


def test_a_low_confidence_extraction_escalates_immediately() -> None:
    observation = Observation(
        ok=False,
        summary="the scan has fields below the confidence threshold 0.80: amount (0.42)",
        error_kind="invalid",
        data={
            "low_confidence": True,
            "low_confidence_fields": ["amount"],
            "threshold": 0.8,
            "fields": {"amount_cents": {"value": 105_000, "confidence": 0.42}},
        },
    )

    decision = classify_failure(
        observation, failed_attempts=0, transient_attempts=0, replans_used=0
    )

    assert decision.action is FailureAction.ESCALATE
    assert "low-confidence" in decision.reason
    assert "amount" in decision.reason
    assert decision.context["low_confidence_fields"] == ["amount"]


def settings_for(tmp_path: Path, ledgerlite_db: Path, maildesk_state) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=maildesk_state.shared_root,
        mail_db=maildesk_state.db_path,
        erp_db=ledgerlite_db,
    )


def run_cli(settings: Settings, script: ScriptedClient, capsys) -> tuple[int, str, RunStore, str]:
    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=script,
    )
    output = capsys.readouterr().out
    store = RunStore(settings.run_db)
    run_id = store.list_runs()[0].id
    return code, output, store, run_id


TRANSIENT_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Validate the Riverstone invoice against its references",
            "allowed_tools": ["erp.get_purchase_order", "erp.get_goods_receipt"],
            "done_criterion": "The amount agrees with the PO and goods receipt",
        },
        {
            "id": "step-2",
            "goal": "File the Riverstone invoice and schedule payment",
            "allowed_tools": ["erp.file_invoice", "erp.schedule_payment"],
            "done_criterion": "RS-2026-088 is filed and the payment scheduled",
        },
        {
            "id": "step-3",
            "goal": "Archive the source document",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}


def transient_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(TRANSIENT_PLAN),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2007"}, call_id="c1"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2507"}, call_id="c2"),
            text_turn("PO and goods receipt agree."),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "RS-2026-088",
                    "vendor_id": "V-1007",
                    "amount": 890.00,
                    "po_id": "PO-2007",
                    "gr_id": "GR-2507",
                },
                call_id="c3",
            ),
            tool_turn("erp.schedule_payment", {"invoice_id": "INV-3003"}, call_id="c4"),
            text_turn("Filed and scheduled."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/RS-2026-088.pdf", "directory": "processed"},
                call_id="c5",
            ),
            text_turn("Archived."),
        ]
    )


def test_the_transient_scenario_retries_and_then_verifies(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = settings_for(tmp_path, ledgerlite_db, maildesk_state)

    code, output, store, run_id = run_cli(settings, transient_script(), capsys)

    run = store.get_run(run_id)
    assert code == 0
    assert run.state is RunState.COMPLETED
    assert "[pass] invoice-filed" in output
    filed = [
        observation
        for observation in store.list_observations(run_id)
        if observation.tool == "erp.file_invoice"
    ]
    assert [(item.ok, item.error_kind, item.attempt) for item in filed] == [
        (False, "transient", 1),
        (True, None, 2),
    ]
    journal = [
        entry for entry in store.list_journal(run_id) if entry.action == "erp.file_invoice"
    ]
    assert [entry.status for entry in journal] == ["done"]
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoices = conn.execute(
            "SELECT * FROM invoices WHERE number = 'RS-2026-088'"
        ).fetchall()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoices[0]["id"],)
        ).fetchall()
    assert len(invoices) == 1
    assert invoices[0]["amount_cents"] == 89_000
    assert [payment["status"] for payment in payments] == ["scheduled"]

    evidence = json.loads(
        (settings.run_db.parent / run_id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["verified"] is True
    file_attempts = [
        item for item in evidence["observations"] if item["tool"] == "erp.file_invoice"
    ]
    assert [item["attempt"] for item in file_attempts] == [1, 2]


def duplicate_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "File the Apex invoice",
                            "allowed_tools": ["erp.file_invoice"],
                            "done_criterion": "AQ-2026-014 is filed",
                        }
                    ]
                }
            ),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "AQ-2026-014",
                    "vendor_id": "V-1002",
                    "amount": 640.00,
                    "po_id": "PO-2002",
                    "gr_id": "GR-2502",
                },
                call_id="c1",
            ),
        ]
    )


def test_a_duplicate_invoice_stops_with_the_existing_record(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = settings_for(tmp_path, ledgerlite_db, maildesk_state)

    code, output, store, run_id = run_cli(settings, duplicate_script(), capsys)

    run = store.get_run(run_id)
    assert code == 1
    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert "duplicate" in escalation.reason
    assert "INV-3002" in escalation.reason
    assert "INV-3002" in escalation.question
    assert "needs_human" in output
    assert "INV-3002" in output
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE number = 'AQ-2026-014' AND vendor_id = 'V-1002'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["scenario"] == "duplicate"


def mismatch_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "File the Cedar invoice",
                            "allowed_tools": ["erp.file_invoice"],
                            "done_criterion": "CD-2026-007 is filed",
                        }
                    ]
                }
            ),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "CD-2026-007",
                    "vendor_id": "V-1003",
                    "amount": 2100.00,
                    "po_id": "PO-2003",
                    "gr_id": "GR-2503",
                },
                call_id="c1",
            ),
        ]
    )


def test_an_amount_mismatch_escalates_with_the_comparison(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = settings_for(tmp_path, ledgerlite_db, maildesk_state)

    code, _output, store, run_id = run_cli(settings, mismatch_script(), capsys)

    run = store.get_run(run_id)
    assert code == 1
    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert "amount mismatch" in escalation.reason
    assert "2,100.00" in escalation.reason
    assert "2,000.00" in escalation.reason
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        filed = conn.execute("SELECT * FROM invoices WHERE number = 'CD-2026-007'").fetchall()
    assert filed == []


def missing_po_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "Match the Bright Path invoice to a purchase order",
                            "allowed_tools": ["erp.list_purchase_orders"],
                            "done_criterion": "The matching purchase order is known",
                        }
                    ]
                }
            ),
            tool_turn("erp.list_purchase_orders", {"vendor_id": "V-1004"}, call_id="c1"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "Invoice BP-2026-123 has no purchase order in LedgerLite",
                    "question": "Which purchase order covers BP-2026-123?",
                },
                call_id="c2",
            ),
        ]
    )


def test_a_missing_purchase_order_escalates_with_a_question(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = settings_for(tmp_path, ledgerlite_db, maildesk_state)

    code, _output, store, run_id = run_cli(settings, missing_po_script(), capsys)

    run = store.get_run(run_id)
    assert code == 1
    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert escalation.question == "Which purchase order covers BP-2026-123?"
    listed = store.list_escalated_runs()
    assert [(summary.id, summary.question) for summary in listed] == [
        (run_id, "Which purchase order covers BP-2026-123?")
    ]
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        filed = conn.execute("SELECT * FROM invoices WHERE number = 'BP-2026-123'").fetchall()
    assert filed == []


def forbidden_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "File the Blocked Supplies invoice",
                            "allowed_tools": ["erp.file_invoice"],
                            "done_criterion": "BL-2026-001 is filed",
                        },
                        {
                            "id": "step-2",
                            "goal": "Schedule the payment",
                            "allowed_tools": ["erp.schedule_payment"],
                            "done_criterion": "The payment is scheduled",
                        },
                    ]
                }
            ),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "BL-2026-001",
                    "vendor_id": "V-1006",
                    "amount": 300.00,
                    "po_id": "PO-2006",
                    "gr_id": "GR-2506",
                },
                call_id="c1",
            ),
            text_turn("BL-2026-001 filed."),
            tool_turn("erp.schedule_payment", {"invoice_id": "INV-3003"}, call_id="c2"),
        ]
    )


def test_a_forbidden_payment_escalates_with_the_policy_reason(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = settings_for(tmp_path, ledgerlite_db, maildesk_state)

    code, _output, store, run_id = run_cli(settings, forbidden_script(), capsys)

    run = store.get_run(run_id)
    assert code == 1
    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert "policy denial" in escalation.reason
    assert "action-rules" in escalation.reason
    assert "blocked" in escalation.reason
    payments = [
        entry for entry in store.list_journal(run_id) if entry.action == "erp.schedule_payment"
    ]
    assert [entry.status for entry in payments] == ["failed"]
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        rows = conn.execute(
            """
            SELECT payments.* FROM payments
            JOIN invoices ON invoices.id = payments.invoice_id
            WHERE invoices.number = 'BL-2026-001'
            """
        ).fetchall()
    assert rows == []
