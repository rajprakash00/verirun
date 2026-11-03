"""End to end: a fixture GST invoice becomes a Zoho Books purchase bill.

The Run ingests a watched-folder invoice, extracts its GST fields, matches the
supplier and purchase order through a read-only Zoho object, parks at the INR
approval gate, and creates exactly one purchase bill after a human yes. The
Verifier reads the same recorded responses through a separate object, checks
the bill against the source PDF, and the Evidence Pack keeps the approval, the
journaled write, and the verification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from tests.india_fixture import (
    INVOICE_NUMBER,
    PDF_FILENAME,
    extraction_json,
    write_invoice_pdf,
)
from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from tests.zoho_fixture import PO_ID as ZOHO_PO_ID
from tests.zoho_fixture import VENDOR_ID, FakeTokens, ZohoBooksFake
from verirun.adapters import WatchedFolderSource
from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoAccountingSystem,
    ZohoBooksClient,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.approve import approve, reject
from verirun.engine.orchestrator import resume_run, run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry, run_policy_facts

REQUEST = "Process the GST invoice in the AP mailbox"
RUN_ID = "RUN-ZOHO-0001"
WORK_ORDER = {
    "sop": "india-gst-extraction",
    "goal": "Create one Zoho Books purchase bill from the GST invoice",
    "assumptions": ["The Zoho Books vendor and purchase order are the source of truth"],
    "systems": ["ledgerlite", "files"],
    "policies": ["india-ap"],
    "approval_gates": [
        {
            "id": "bill-approval-threshold",
            "description": "A purchase bill above 10,000 INR waits for a human",
            "policy": "india-ap",
        },
        {
            "id": "missing-purchase-order-needs-approval",
            "description": "A bill without its purchase order waits for a human",
            "policy": "india-ap",
        },
        {
            "id": "low-confidence-extraction-needs-approval",
            "description": "A low-confidence extraction waits for a human",
            "policy": "india-ap",
        },
        {
            "id": "bill-vendor-differs-from-extraction",
            "description": "A bill for a different or unmatched vendor waits for a human",
            "policy": "india-ap",
        },
    ],
    "success_criteria": [
        "Exactly one purchase bill exists in Zoho Books",
        "The bill agrees with the source invoice",
        "The source invoice is archived in processed",
    ],
    "open_questions": [],
}
PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and extract its India GST fields",
            "allowed_tools": ["mail.read", "invoice.extract"],
            "done_criterion": "Every field has a confidence and a citation",
        },
        {
            "id": "step-2",
            "goal": "Match the supplier and purchase order in Zoho Books",
            "allowed_tools": [
                "erp.list_vendors",
                "erp.list_purchase_orders",
                "erp.get_purchase_order",
            ],
            "done_criterion": "The vendor and purchase order are identified",
        },
        {
            "id": "step-3",
            "goal": "Create the purchase bill after approval",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "Exactly one purchase bill exists",
        },
        {
            "id": "step-4",
            "goal": "Archive the source invoice",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}


def zoho_clients(fake: ZohoBooksFake) -> tuple[ZohoAccountingReads, ZohoAccountingSystem]:
    """The Verifier's read object and the executor's write object.

    They share the recorded HTTP responses but hold different clients, mirroring
    the separate read-only and write OAuth tokens in the product path.
    """
    def client() -> ZohoBooksClient:
        return ZohoBooksClient(
            token_manager=FakeTokens(),  # type: ignore[arg-type]
            organization_id="6000000000001",
            base_url="https://www.zohoapis.in/books/v3",
            http=httpx.Client(transport=httpx.MockTransport(fake)),
            sleep=lambda seconds: None,
        )

    reads = ZohoAccountingReads(client())
    write_reads = ZohoAccountingReads(client())
    return reads, ZohoAccountingSystem(write_reads)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
        mail_state_db=tmp_path / "mail-index.db",
    )


def script(message_id: str, attachment_path: str) -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(PLAN),
            tool_turn("mail.read", {"message_id": message_id}, call_id="c1"),
            tool_turn("invoice.extract", {"path": attachment_path}, call_id="c2"),
            text_turn(extraction_json()),
            text_turn("Extracted the invoice with citations."),
            tool_turn("erp.list_vendors", {"status": "active"}, call_id="c3"),
            tool_turn("erp.list_purchase_orders", {"vendor_id": VENDOR_ID}, call_id="c4"),
            tool_turn("erp.get_purchase_order", {"po_id": ZOHO_PO_ID}, call_id="c5"),
            text_turn("The vendor and purchase order match."),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": INVOICE_NUMBER,
                    "vendor_id": VENDOR_ID,
                    "amount": 11800.00,
                    "currency": "INR",
                    "po_id": ZOHO_PO_ID,
                },
                call_id="c6",
            ),
            # Consumed after the human approves the prepared bill.
            text_turn("Created the purchase bill after approval."),
            tool_turn(
                "files.archive",
                {"path": attachment_path, "directory": "processed"},
                call_id="c7",
            ),
            text_turn("Archived the source invoice."),
        ]
    )


def run_scenario(
    tmp_path: Path, fake: ZohoBooksFake, *, run_id: str = RUN_ID
) -> tuple[Any, dict, RunStore, ScriptedClient, Any]:
    watch = tmp_path / "watch"
    watch.mkdir()
    write_invoice_pdf(watch / PDF_FILENAME)
    chosen = settings(tmp_path)
    mail = WatchedFolderSource(watch, chosen.shared_dir, chosen.mail_state_db)
    assert mail.sync() == 1
    summary = mail.list_messages()[0]
    message = mail.read_message(summary.id)
    attachment = message.attachments[0]

    reads, write = zoho_clients(fake)
    backends = Backends(mail=mail, accounting=write, reads=reads)
    context = load_company_context(chosen.company_dir)
    pack = load_task_pack(ROOT / "tasks" / "invoice-processing-in.yaml")
    store = RunStore(chosen.run_db)
    llm = script(summary.id, attachment.path)
    registry = build_registry(
        chosen,
        context,
        pack,
        llm,
        backends=backends,
        policy_facts=run_policy_facts(
            store, run_id, pack, accounting=backends.accounting
        ),
    )

    run = run_task(
        REQUEST,
        pack,
        context,
        llm,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=mail.document_root,
        evidence_root=chosen.run_db.parent,
        run_id=run_id,
    )
    evidence = json.loads(
        (chosen.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    return run, evidence, store, llm, (chosen, pack, backends, registry, mail)


def test_a_bill_over_the_threshold_parks_then_is_created_once(
    tmp_path: Path,
) -> None:
    fake = ZohoBooksFake()
    run, evidence, store, llm, (chosen, pack, backends, registry, mail) = run_scenario(
        tmp_path, fake
    )

    assert run.state is RunState.AWAITING_APPROVAL
    request = store.open_approval(run.id)
    assert request is not None
    assert request.tool == "erp.file_invoice"
    assert request.action == "bill.create"
    assert request.policy == "india-ap"
    assert request.rule == "bill-approval-threshold"
    assert request.arguments["number"] == INVOICE_NUMBER
    assert fake.posts == []

    # A pending approval is never submitted by time or by a resume.
    run = resume_run(
        run.id,
        pack,
        llm,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=mail.document_root,
        evidence_root=chosen.run_db.parent,
    )
    assert run.state is RunState.AWAITING_APPROVAL
    assert fake.posts == []

    approve(store, request.id)
    run = resume_run(
        run.id,
        pack,
        llm,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=mail.document_root,
        evidence_root=chosen.run_db.parent,
    )

    assert run.state is RunState.COMPLETED, run.error
    assert len(fake.posts) == 1
    assert fake.posts[0]["bill_number"] == INVOICE_NUMBER
    assert fake.posts[0]["vendor_id"] == VENDOR_ID
    assert fake.posts[0]["purchaseorder_ids"] == [ZOHO_PO_ID]
    assert fake.posts[0]["reference_number"].startswith(
        "verirun:step-3:erp.file_invoice:"
    )
    created = [
        bill for bill in fake.bills if bill["bill_number"] == INVOICE_NUMBER
    ]
    assert len(created) == 1

    evidence = json.loads(
        (chosen.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["verified"] is True
    assert [check["id"] for check in evidence["verification"]["checks"]] == [
        "bill-filed",
        "accounting-invoice-matches",
        "source-archived",
    ]
    assert all(check["ok"] for check in evidence["verification"]["checks"])
    approval = evidence["approvals"][0]
    assert approval["status"] == "approved"
    assert approval["executed_at"] is not None
    write = next(
        item for item in evidence["action_log"] if item["action"] == "erp.file_invoice"
    )
    assert write["status"] == "done"
    assert write["key"].startswith("step-3:erp.file_invoice:")
    bill_check = evidence["verification"]["checks"][0]
    assert f"erp:{fake.bills[-1]['bill_id']}" in bill_check["evidence"]
    assert any(item.startswith("source:") for item in bill_check["evidence"])
    assert "gstin:29AABCV1234C1ZQ" in bill_check["evidence"]


def test_rejecting_the_bill_aborts_without_a_write(tmp_path: Path) -> None:
    fake = ZohoBooksFake()
    run, _evidence, store, _llm, _state = run_scenario(
        tmp_path, fake, run_id="RUN-ZOHO-0002"
    )
    request = store.open_approval(run.id)
    assert request is not None

    reject(store, request.id, "The supplier GSTIN is under review")

    run = store.get_run(run.id)
    assert run.state is RunState.FAILED
    assert run.error is not None and "under review" in run.error
    assert fake.posts == []
    assert store.get_approval(request.id).decision_reason == "The supplier GSTIN is under review"
    assert store.get_approval(request.id).executed_at is None
