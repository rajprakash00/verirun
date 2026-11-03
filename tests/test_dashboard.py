"""HTTP tests for the local dashboard: run list, run detail, approval queue.

The dashboard is the human surface of a Run: the Work Order, the Plan, a live
timeline of steps and observations, escalations, and the approval queue.
Decisions post here and either resume or abort the parked Run.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from mocks.ledgerlite import db as ledgerlite
from tests.india_fixture import GSTIN, INVOICE_NUMBER, PDF_FILENAME, SUPPLIER
from tests.support import (
    ROOT,
    WORK_ORDER,
    ScriptedClient,
    park_over_limit_run,
    run_settings,
    tool_turn,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.models import Observation
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry
from verirun.web import create_app

ESCALATED_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Match the Bright Path invoice to a purchase order",
            "allowed_tools": ["erp.list_purchase_orders"],
            "done_criterion": "The matching purchase order is known",
        }
    ]
}


def escalate_run(tmp_path: Path, ledgerlite_db: Path, maildesk_state) -> tuple[object, RunStore, str]:
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    store = RunStore(settings.run_db)
    script = ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(ESCALATED_PLAN),
            tool_turn(
                "task.escalate",
                {
                    "reason": "Invoice BP-2026-123 has no purchase order in LedgerLite",
                    "question": "Which purchase order covers BP-2026-123?",
                },
                call_id="c1",
            ),
        ]
    )
    backends = Backends.mock_suite(settings)
    run = run_task(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        script,
        store,
        build_registry(settings, context, task_pack, script, backends=backends),
        accounting=backends.verifier_accounting,
        shared_root=backends.mail.document_root,
        evidence_root=settings.run_db.parent,
    )
    return settings, store, run.id


def test_the_run_list_shows_every_run_state(tmp_path: Path, ledgerlite_db: Path, maildesk_state) -> None:
    settings, _store, _script, parked_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    _settings, _store, escalated_id = escalate_run(tmp_path, ledgerlite_db, maildesk_state)
    client = TestClient(create_app(settings))

    page = client.get("/")

    assert page.status_code == 200
    assert parked_id in page.text
    assert escalated_id in page.text
    assert "awaiting_approval" in page.text
    assert "needs_human" in page.text
    assert "Which purchase order covers BP-2026-123?" in page.text


def test_the_run_page_shows_the_work_order_plan_and_timeline(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, _store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    client = TestClient(create_app(settings))

    page = client.get(f"/runs/{run_id}")

    assert page.status_code == 200
    assert "Work Order" in page.text
    assert "SI-2026-550" in page.text
    assert "Plan" in page.text
    assert "step-2" in page.text
    assert "erp.schedule_payment" in page.text
    assert "requires approval" in page.text
    assert "awaiting_approval" in page.text


def test_the_timeline_fragment_shows_steps_observations_and_the_gate(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, _store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    client = TestClient(create_app(settings))

    fragment = client.get(f"/runs/{run_id}/timeline")

    assert fragment.status_code == 200
    assert "step-1" in fragment.text
    assert "erp.file_invoice" in fragment.text
    assert "erp.schedule_payment" in fragment.text
    assert "requires approval" in fragment.text
    assert "pending" in fragment.text


def test_the_approval_queue_shows_the_prepared_action(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, _store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    client = TestClient(create_app(settings))

    page = client.get("/approvals")

    assert page.status_code == 200
    assert run_id in page.text
    assert "erp.schedule_payment" in page.text
    assert "payment.schedule" in page.text
    assert "spend-limits" in page.text
    assert "INV-3003" in page.text
    assert "Approve" in page.text
    assert "Reject" in page.text


def test_approving_over_http_resumes_and_completes_the_run(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, store, script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    request = store.open_approval(run_id)
    assert request is not None
    client = TestClient(create_app(settings, client_factory=lambda _run_id: script))

    response = client.post(f"/approvals/{request.id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    run = store.get_run(run_id)
    assert run.state is RunState.COMPLETED
    assert store.get_approval(request.id).executed_at is not None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'SI-2026-550'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert [payment["status"] for payment in payments] == ["scheduled"]
    page = client.get(f"/runs/{run_id}")
    assert "completed" in page.text
    assert "payment-scheduled" in page.text
    assert ">pass</span>" in page.text


def test_rejecting_over_http_aborts_with_the_recorded_reason(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    request = store.open_approval(run_id)
    assert request is not None
    client = TestClient(create_app(settings))

    response = client.post(
        f"/approvals/{request.id}/reject",
        data={"reason": "Budget is frozen until Q4"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    run = store.get_run(run_id)
    assert run.state is RunState.FAILED
    assert run.error is not None and "Budget is frozen until Q4" in run.error
    decided = store.get_approval(request.id)
    assert decided.status == "rejected"
    assert decided.decision_reason == "Budget is frozen until Q4"
    page = client.get(f"/runs/{run_id}")
    assert "Budget is frozen until Q4" in page.text


def test_a_pending_approval_never_auto_approves_with_time(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state, monkeypatch
) -> None:
    settings, store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    client = TestClient(create_app(settings))

    moved = datetime.now(UTC) + timedelta(days=3)
    monkeypatch.setattr("verirun.runs.store._now", lambda: moved)
    client.get("/approvals")
    client.get(f"/runs/{run_id}")

    assert store.get_run(run_id).state is RunState.AWAITING_APPROVAL
    request = store.open_approval(run_id)
    assert request is not None
    assert request.status == "pending"
    assert request.decided_at is None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        payments = conn.execute(
            """
            SELECT payments.* FROM payments
            JOIN invoices ON invoices.id = payments.invoice_id
            WHERE invoices.number = 'SI-2026-550'
            """
        ).fetchall()
    assert payments == []


def test_an_escalated_run_shows_its_question_on_the_page(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, _store, run_id = escalate_run(tmp_path, ledgerlite_db, maildesk_state)
    client = TestClient(create_app(settings))

    page = client.get(f"/runs/{run_id}")

    assert page.status_code == 200
    assert "Escalation" in page.text
    assert "Invoice BP-2026-123 has no purchase order in LedgerLite" in page.text
    assert "Which purchase order covers BP-2026-123?" in page.text


def seed_india_approval(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
    )
    store = RunStore(settings.run_db)
    run_id = "RUN-IN-UI"
    store.create_run(
        "Process the tax invoice in the AP mailbox",
        "invoice-processing-in",
        run_id=run_id,
    )
    for target in (RunState.RESOLVING, RunState.PLANNED, RunState.EXECUTING):
        store.transition(run_id, target)
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Read the invoice email",
            data={
                "id": "MSG-1",
                "folder": "inbox",
                "sender": "Paper Trail Accounts",
                "sender_email": "accounts@papertrail.example",
                "subject": f"Tax invoice {INVOICE_NUMBER} from {SUPPLIER}",
                "attachments": [
                    {
                        "id": "sha256:abc",
                        "filename": PDF_FILENAME,
                        "path": f"attachments/abc/{PDF_FILENAME}",
                    }
                ],
            },
        ),
        tool="mail.read",
    )
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Extracted the invoice with citations",
            data={
                "path": f"attachments/abc/{PDF_FILENAME}",
                "min_confidence": 0.95,
                "threshold": 0.8,
                "low_confidence": False,
                "fields": {
                    "supplier_name": {
                        "value": SUPPLIER,
                        "confidence": 0.98,
                        "citation": f"Supplier: {SUPPLIER}",
                    },
                    "supplier_gstin": {
                        "value": GSTIN,
                        "confidence": 0.99,
                        "citation": f"GSTIN: {GSTIN}",
                    },
                    "invoice_number": {
                        "value": INVOICE_NUMBER,
                        "confidence": 0.99,
                        "citation": f"Invoice number: {INVOICE_NUMBER}",
                    },
                    "total_paise": {
                        "value": 1_180_000,
                        "confidence": 0.98,
                        "citation": "Total: 11,800.00 INR",
                    },
                    "currency": {
                        "value": "INR",
                        "confidence": 0.99,
                        "citation": "Total: 11,800.00 INR",
                    },
                },
                "validations": [
                    {
                        "id": "gstin_checksum",
                        "ok": True,
                        "detail": f"GSTIN {GSTIN} is well formed",
                    },
                    {
                        "id": "tax_sum",
                        "ok": True,
                        "detail": "taxable 10,000.00 INR + SGST 900.00 INR = 11,800.00 INR",
                    },
                ],
            },
        ),
        tool="invoice.extract",
    )
    store.create_approval(
        run_id,
        key="step-3:erp.file_invoice:abc",
        step_position=2,
        step_id="step-3",
        tool="erp.file_invoice",
        action="bill.create",
        arguments={
            "number": INVOICE_NUMBER,
            "vendor_id": "V-IN-1001",
            "amount": 11800.00,
            "currency": "INR",
            "po_id": "PO-IN-2001",
        },
        policy="india-ap",
        rule="bill-approval-threshold",
        reason="A purchase bill above 10,000.00 INR must be approved by a human",
    )
    store.transition(run_id, RunState.AWAITING_APPROVAL)
    return settings


def test_the_approval_review_shows_the_source_extraction_and_proposed_bill(
    tmp_path: Path,
) -> None:
    settings = seed_india_approval(tmp_path)
    client = TestClient(create_app(settings))

    queue = client.get("/approvals")
    run_page = client.get("/runs/RUN-IN-UI")

    for page in (queue, run_page):
        assert page.status_code == 200
        assert f"Tax invoice {INVOICE_NUMBER} from {SUPPLIER}" in page.text
        assert PDF_FILENAME in page.text
        assert SUPPLIER in page.text
        assert GSTIN in page.text
        assert INVOICE_NUMBER in page.text
        assert "GST validations" in page.text
        assert "gstin_checksum" in page.text
        assert "Proposed purchase bill" in page.text
        assert "11800.0" in page.text
        assert "PO-IN-2001" in page.text
        assert "Approve" in page.text
        assert "Reject" in page.text


def test_unknown_runs_and_approvals_return_404(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, _store, _script, _run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    client = TestClient(create_app(settings))

    assert client.get("/runs/RUN-NOPE").status_code == 404
    assert client.post("/approvals/999/approve").status_code == 404
    assert client.post("/approvals/999/reject", data={"reason": "nope"}).status_code == 404
