"""The engine runs one whole Run against the in-memory accounting fake.

No LedgerLite database is involved: the executor reads and writes through
``AccountingSystem``, and the Verifier reads the same fake through
``AccountingReads``. The source document and the archive are real files.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.support import ROOT, WORK_ORDER, ScriptedClient, text_turn, tool_turn
from verirun.adapters import (
    GoodsReceipt,
    InMemoryAccountingSystem,
    MockMailSource,
    PurchaseOrder,
    Vendor,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry

CREATED_AT = "2026-09-01T09:00:00+00:00"
FILED_INVOICE_ID = "INV-3001"

PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Validate the Northwind invoice against its references",
            "allowed_tools": ["erp.get_purchase_order", "erp.get_goods_receipt"],
            "done_criterion": "The amount agrees with the PO and goods receipt",
        },
        {
            "id": "step-2",
            "goal": "File the Northwind invoice and schedule payment",
            "allowed_tools": ["erp.file_invoice", "erp.schedule_payment"],
            "done_criterion": "NW-2026-001 is filed and the payment scheduled",
        },
        {
            "id": "step-3",
            "goal": "Archive the source document",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}


def in_memory_accounting() -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id="V-1001",
                name="Northwind Traders",
                tax_id="TAX-1001",
                email="billing@northwind.example",
                address="1 North Way",
                status="active",
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                id="PO-2001",
                vendor_id="V-1001",
                amount_cents=125_000,
                currency="USD",
                status="open",
                issued_at=CREATED_AT,
            )
        ],
        goods_receipts=[
            GoodsReceipt(
                id="GR-2501",
                po_id="PO-2001",
                amount_cents=125_000,
                currency="USD",
                status="received",
                received_at=CREATED_AT,
            )
        ],
    )


def script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(PLAN),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c1"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, call_id="c2"),
            text_turn("PO and goods receipt agree."),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "NW-2026-001",
                    "vendor_id": "V-1001",
                    "amount": 1250.00,
                    "po_id": "PO-2001",
                    "gr_id": "GR-2501",
                },
                call_id="c3",
            ),
            tool_turn(
                "erp.schedule_payment",
                {"invoice_id": FILED_INVOICE_ID, "scheduled_for": "2026-10-05"},
                call_id="c4",
            ),
            text_turn("Filed and scheduled."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/NW-2026-001.pdf", "directory": "processed"},
                call_id="c5",
            ),
            text_turn("Archived."),
        ]
    )


def test_the_engine_completes_against_the_in_memory_accounting_fake(
    tmp_path: Path, maildesk_state
) -> None:
    shared_root = tmp_path / "fake-shared"
    source = shared_root / "documents" / "invoices" / "NW-2026-001.pdf"
    source.parent.mkdir(parents=True)
    shutil.copy2(
        maildesk_state.shared_root / "documents" / "invoices" / "NW-2026-001.pdf",
        source,
    )
    settings = Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=shared_root,
        mail_db=maildesk_state.db_path,
        erp_db=tmp_path / "unused-ledgerlite.db",
    )
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    store = RunStore(settings.run_db)
    accounting = in_memory_accounting()
    backends = Backends(
        mail=MockMailSource(settings.mail_db, shared_root), accounting=accounting
    )
    llm = script()

    run = run_task(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        llm,
        store,
        build_registry(settings, context, task_pack, llm, backends=backends),
        accounting=backends.verifier_accounting,
        shared_root=backends.mail.document_root,
        evidence_root=settings.run_db.parent,
    )

    assert run.state is RunState.COMPLETED
    checks = {check.id: check for check in store.get_verification(run.id)}
    assert set(checks) == {"invoice-filed", "payment-scheduled", "source-archived"}
    assert all(check.ok for check in checks.values())
    assert f"erp:{FILED_INVOICE_ID}" in checks["invoice-filed"].evidence

    bills = accounting.list_bills(number="NW-2026-001")
    assert len(bills) == 1
    assert bills[0].amount_cents == 125_000
    payments = accounting.list_payments(FILED_INVOICE_ID)
    assert [payment.amount_cents for payment in payments] == [125_000]
    assert (shared_root / "processed" / "NW-2026-001.pdf").is_file()
