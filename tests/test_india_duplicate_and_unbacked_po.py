"""End-to-end regressions from the step-6 live check.

Both cases replay what the live model actually did, not what the seeded eval
script does:

1. A duplicate invoice: the model sees the existing bill through
   ``erp.list_invoices`` and archives the copy without calling
   ``erp.file_invoice``. The Verifier must still detect the duplicate and park
   the Run in ``needs_human`` with a duplicate escalation, leaving exactly one
   bill.
2. A no-PO invoice: the model proposes an unrelated purchase order. The
   missing-PO gate still fires, the prepared Approval Request drops the
   unbacked ``po_id``, and an approval writes a bill without it.

The in-memory mail and accounting fakes need no Mock Suite database, Zoho
credential, network, or recorded replay fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from verirun.adapters import (
    Bill,
    InMemoryAccountingSystem,
    PurchaseOrder,
    Vendor,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.approve import approve
from verirun.engine.orchestrator import resume_run, run_task
from verirun.engine.states import RunState
from verirun.eval.india import (
    CREATED_AT,
    INDIA_CASES,
    SUPPLIER,
    VENDOR_EMAIL,
    VENDOR_ID,
    build_india_backends,
    extraction_json,
)
from verirun.eval.scenarios import INDIA_CHECKS, INDIA_PLAN, india_work_order
from verirun.runs.models import Run
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry, run_policy_facts

UNRELATED_PO = "PO-IN-2001"


def india_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
        mail_state_db=tmp_path / "mail-index.db",
    )


def duplicate_plan() -> dict:
    """The eval plan, with the duplicate read allowed before any write."""
    plan = json.loads(json.dumps(INDIA_PLAN))
    plan["steps"][2]["allowed_tools"] = ["erp.list_invoices", "erp.file_invoice"]
    return plan


def drive_run(
    tmp_path: Path,
    *,
    request: str,
    plan: dict,
    replies: list,
    run_id: str,
    backends: Backends,
) -> tuple[RunStore, Run]:
    settings = india_settings(tmp_path)
    context = load_company_context(settings.company_dir)
    pack = load_task_pack(settings.tasks_dir / "invoice-processing-in.yaml")
    store = RunStore(settings.run_db)
    script = ScriptedClient(
        [
            json.dumps(india_work_order(request)),
            json.dumps(plan),
            *replies,
        ]
    )
    registry = build_registry(
        settings,
        context,
        pack,
        script,
        backends=backends,
        policy_facts=run_policy_facts(
            store, run_id, pack, accounting=backends.accounting
        ),
    )
    run = run_task(
        request,
        pack,
        context,
        script,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=backends.mail.document_root,
        evidence_root=settings.run_db.parent,
        run_id=run_id,
    )
    return store, run


def test_a_duplicate_is_escalated_when_the_agent_never_files_the_bill(
    tmp_path: Path,
) -> None:
    """Re-ingesting an already-billed PDF ends `needs_human`, not `failed`."""
    case = INDIA_CASES["india_duplicate"]
    backends = build_india_backends(case, tmp_path)
    request = f"Process the tax invoice {case.invoice_number} from the AP mailbox"

    store, run = drive_run(
        tmp_path,
        request=request,
        plan=duplicate_plan(),
        run_id="RUN-IN-DUPLICATE",
        backends=backends,
        replies=[
            tool_turn("mail.read", {"message_id": case.message_id}, call_id="c1"),
            tool_turn("invoice.extract", {"path": case.attachment_path}, call_id="c2"),
            text_turn(extraction_json(case)),
            text_turn("Extracted the invoice with citations and GST validations."),
            tool_turn("erp.list_vendors", {"status": "active"}, call_id="c3"),
            tool_turn(
                "erp.list_purchase_orders", {"vendor_id": VENDOR_ID}, call_id="c4"
            ),
            tool_turn(
                "erp.get_purchase_order", {"po_id": case.po_reference}, call_id="c5"
            ),
            text_turn("The supplier and purchase order are known."),
            # The live model found the existing bill and stopped before writing.
            tool_turn(
                "erp.list_invoices", {"number": case.invoice_number}, call_id="c6"
            ),
            text_turn(
                f"{case.invoice_number} already has a live bill; the copy must not "
                "be filed again."
            ),
            tool_turn(
                "files.archive",
                {"path": case.attachment_path, "directory": "processed"},
                call_id="c7",
            ),
            text_turn("Archived the duplicate copy."),
        ],
    )

    assert run.state is RunState.NEEDS_HUMAN, run.error
    assert run.error is None

    # Every verification check ran, so the Evidence Pack carries the reason.
    verification = {item.id: item for item in store.get_verification(run.id)}
    assert set(verification) == set(INDIA_CHECKS)
    assert not verification["bill-filed"].ok
    assert not verification["accounting-invoice-matches"].ok
    assert "already has a bill" in verification["accounting-invoice-matches"].detail
    assert "INV-IN-3001" in verification["accounting-invoice-matches"].detail
    assert "received" in verification["accounting-invoice-matches"].detail
    assert verification["source-archived"].ok
    assert verification["extraction-confidence"].ok

    escalations = store.list_escalations(run.id)
    assert len(escalations) == 1
    escalation = escalations[0]
    assert escalation.reason.startswith("duplicate invoice: ")
    assert case.invoice_number in escalation.reason
    assert "INV-IN-3001" in escalation.reason
    assert "should this invoice be ignored" in escalation.question.lower()
    assert escalation.context["duplicate"] is True

    # Exactly the bill that already existed: the copy was never written.
    bills = backends.accounting.list_bills()
    assert [(bill.id, bill.number) for bill in bills] == [
        ("INV-IN-3001", case.invoice_number)
    ]
    assert not [
        entry
        for entry in store.list_journal(run.id)
        if entry.action == "erp.file_invoice"
    ]

    evidence = json.loads(
        (tmp_path / "runs" / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["state"] == "needs_human"
    assert evidence["error"] is None
    assert any(
        "should this invoice be ignored" in question.lower()
        for question in evidence["open_questions"]
    )
    assert "erp.file_invoice" not in [
        entry["action"] for entry in evidence["action_log"]
    ]


def accounting_with_an_unrelated_billed_order(case) -> InMemoryAccountingSystem:
    """The live situation: a fresh no-PO invoice beside an already-billed PO."""
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id=VENDOR_ID,
                name=SUPPLIER,
                tax_id=case.vendor_gstin,
                email=VENDOR_EMAIL,
                address="14 Market Road, Bengaluru",
                status="active",
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                id=UNRELATED_PO,
                number=UNRELATED_PO,
                vendor_id=VENDOR_ID,
                amount_cents=1_180_000,
                currency="INR",
                status="open",
                issued_at=CREATED_AT,
            )
        ],
        bills=[
            Bill(
                id="INV-IN-3002",
                number="PTS/2026/0142",
                vendor_id=VENDOR_ID,
                po_id=UNRELATED_PO,
                gr_id=None,
                amount_cents=1_180_000,
                currency="INR",
                status="received",
                received_at=CREATED_AT,
            )
        ],
    )


def test_a_no_po_approval_drops_the_unbacked_purchase_order(
    tmp_path: Path,
) -> None:
    """The prepared action and the approved write never carry a fake PO."""
    case = INDIA_CASES["india_missing_po"]
    seeded = build_india_backends(case, tmp_path)
    accounting = accounting_with_an_unrelated_billed_order(case)
    backends = Backends(mail=seeded.mail, accounting=accounting)
    request = f"Process the tax invoice {case.invoice_number} from the AP mailbox"

    settings = india_settings(tmp_path)
    context = load_company_context(settings.company_dir)
    pack = load_task_pack(settings.tasks_dir / "invoice-processing-in.yaml")
    store = RunStore(settings.run_db)
    script = ScriptedClient(
        [
            json.dumps(india_work_order(request)),
            json.dumps(INDIA_PLAN),
            tool_turn("mail.read", {"message_id": case.message_id}, call_id="c1"),
            tool_turn("invoice.extract", {"path": case.attachment_path}, call_id="c2"),
            text_turn(extraction_json(case)),
            text_turn("Extracted the invoice with citations and GST validations."),
            tool_turn("erp.list_vendors", {"status": "active"}, call_id="c3"),
            tool_turn(
                "erp.list_purchase_orders", {"vendor_id": VENDOR_ID}, call_id="c4"
            ),
            text_turn("No purchase order is printed on this invoice."),
            # The live model invented a PO it had seen in the system.
            tool_turn(
                "erp.file_invoice",
                {
                    "number": case.invoice_number,
                    "vendor_id": VENDOR_ID,
                    "amount": case.total_paise / 100,
                    "currency": "INR",
                    "po_id": UNRELATED_PO,
                },
                call_id="c5",
            ),
            # Consumed after the human approves the prepared bill.
            text_turn("Created the purchase bill."),
            tool_turn(
                "files.archive",
                {"path": case.attachment_path, "directory": "processed"},
                call_id="c6",
            ),
            text_turn("Archived the source invoice."),
        ]
    )
    registry = build_registry(
        settings,
        context,
        pack,
        script,
        backends=backends,
        policy_facts=run_policy_facts(
            store, "RUN-IN-NO-PO", pack, accounting=backends.accounting
        ),
    )
    run = run_task(
        request,
        pack,
        context,
        script,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=backends.mail.document_root,
        evidence_root=settings.run_db.parent,
        run_id="RUN-IN-NO-PO",
    )

    assert run.state is RunState.AWAITING_APPROVAL, run.error
    request_record = store.open_approval(run.id)
    assert request_record is not None
    assert request_record.rule == "missing-purchase-order-needs-approval"
    assert "purchase order" in request_record.reason.lower()
    assert "po_id" not in request_record.arguments
    assert request_record.arguments == {
        "number": case.invoice_number,
        "vendor_id": VENDOR_ID,
        "amount": case.total_paise / 100,
        "currency": "INR",
    }

    # The model was told which proposed argument was dropped.
    policy_observation = next(
        item
        for item in store.list_observations(run.id)
        if item.tool == "erp.file_invoice" and item.data.get("approval_required")
    )
    assert policy_observation.data["unbacked_arguments"] == ["po_id"]
    assert "po_id" not in policy_observation.data["arguments"]

    # Nothing was written while parked, so the unrelated bill is still the only one.
    assert [bill.number for bill in accounting.list_bills()] == ["PTS/2026/0142"]

    # The human still sees the source, extraction, validations, and reason.
    evidence = json.loads(
        (settings.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    approval = evidence["approvals"][0]
    assert approval["rule"] == "missing-purchase-order-needs-approval"
    assert approval["reason"]
    assert "po_id" not in approval["arguments"]
    assert evidence["sources"] and evidence["sources"][0]["attachments"]
    assert evidence["extractions"]
    assert evidence["validations"]
    assert all(item["ok"] for item in evidence["validations"])

    approve(store, request_record.id)
    run = resume_run(
        run.id,
        pack,
        script,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=backends.mail.document_root,
        evidence_root=settings.run_db.parent,
    )

    assert run.state is RunState.COMPLETED, run.error
    bills = {
        bill.number: bill for bill in accounting.list_bills() if bill.id != "INV-IN-3002"
    }
    assert list(bills) == [case.invoice_number]
    assert bills[case.invoice_number].po_id is None
    assert bills[case.invoice_number].amount_cents == case.total_paise
    assert all(
        result.ok for result in store.get_verification(run.id)
    ), [(result.id, result.detail) for result in store.get_verification(run.id)]
    assert store.list_approvals(run.id)[0].executed_at is not None
