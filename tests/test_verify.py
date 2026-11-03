"""Tests for the Verifier: independent checks against LedgerLite and the filesystem.

Ground truth is seeded by the mocks. The Run's action journal is written the way
the executor writes it, then the Verifier inspects real state only.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from mocks.ledgerlite import db as ledgerlite
from tests.india_fixture import (
    GSTIN,
    INVOICE_NUMBER,
    PDF_FILENAME,
    PO_ID,
    SUPPLIER,
    TOTAL_PAISE,
    write_invoice_pdf,
)
from tests.support import ROOT, ScriptedClient, vision_fields
from verirun.adapters import (
    Bill,
    InMemoryAccountingSystem,
    MockAccountingSystem,
    PurchaseOrder,
    Vendor,
)
from verirun.context.task_pack import TaskPack, VerificationCheck, load_task_pack
from verirun.engine.models import Observation
from verirun.engine.states import RunState
from verirun.engine.verify import verify_run
from verirun.runs.store import RunStore
from verirun.tools import ToolRegistry, build_erp_tools, build_file_tools

CREATED_AT = "2026-09-01T09:00:00+00:00"

INVOICE = {
    "number": "NW-2026-001",
    "vendor_id": "V-1001",
    "amount": 1250.00,
    "po_id": "PO-2001",
    "gr_id": "GR-2501",
}

SCAN = {
    "number": "PP-2026-042",
    "vendor_id": "V-1008",
    "amount": 1050.00,
    "po_id": "PO-2008",
    "gr_id": "GR-2508",
}


def task_pack() -> TaskPack:
    return load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")


def seed_verifying_run(store: RunStore, run_id: str = "RUN-0001") -> None:
    store.create_run("Process the invoices in the AP mailbox", "invoice-processing", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)
    store.transition(run_id, RunState.VERIFYING)


def file_invoice(
    store: RunStore,
    erp_db: Path,
    *,
    run_id: str = "RUN-0001",
    key: str = "file-1",
    **overrides: Any,
) -> str:
    arguments = {**INVOICE, **overrides}
    tools = ToolRegistry(build_erp_tools(MockAccountingSystem(erp_db)), allowlist=["erp.file_invoice"])
    observation = tools.invoke("erp.file_invoice", arguments)
    assert observation.ok, observation.summary
    store.record_action(run_id, key, "erp.file_invoice", arguments)
    store.complete_action(run_id, key, observation.model_dump())
    return observation.data["invoice_id"]


def file_invoice_directly(
    store: RunStore,
    erp_db: Path,
    *,
    amount_cents: int,
    run_id: str = "RUN-0001",
    key: str = "file-1",
) -> str:
    """Seed an ERP row without the write tool.

    The Verifier must catch wrong ground truth even when it was not written by
    this Run's executor, so this test setup bypasses the tool that would have
    rejected the amount.
    """
    with ledgerlite.connect(erp_db) as conn:
        conn.execute(
            """
            INSERT INTO invoices
                (id, number, vendor_id, po_id, gr_id, amount_cents, status, received_at, scenario)
            VALUES ('INV-9001', 'NW-2026-001', 'V-1001', 'PO-2001', 'GR-2501', ?, 'received',
                    '2026-09-05T09:00:00+00:00', 'filed')
            """,
            (amount_cents,),
        )
        conn.commit()
    arguments = {"number": "NW-2026-001", "vendor_id": "V-1001", "amount": amount_cents / 100}
    store.record_action(run_id, key, "erp.file_invoice", arguments)
    store.complete_action(
        run_id,
        key,
        {"invoice_id": "INV-9001", "number": "NW-2026-001", "vendor_id": "V-1001"},
    )
    return "INV-9001"


def schedule_payment(store: RunStore, erp_db: Path, invoice_id: str, *, run_id: str = "RUN-0001") -> None:
    tools = ToolRegistry(build_erp_tools(MockAccountingSystem(erp_db)), allowlist=["erp.schedule_payment"])
    observation = tools.invoke(
        "erp.schedule_payment", {"invoice_id": invoice_id, "scheduled_for": "2026-10-05"}
    )
    assert observation.ok, observation.summary
    store.record_action(run_id, "pay-1", "erp.schedule_payment", {"invoice_id": invoice_id})
    store.complete_action(run_id, "pay-1", observation.model_dump())


def archive_source(
    store: RunStore,
    shared_root: Path,
    number: str,
    *,
    run_id: str = "RUN-0001",
    directory: str = "processed",
) -> None:
    tools = ToolRegistry(build_file_tools(shared_root), allowlist=["files.archive"])
    arguments = {"path": f"documents/invoices/{number}.pdf", "directory": directory}
    observation = tools.invoke("files.archive", arguments)
    assert observation.ok, observation.summary
    store.record_action(run_id, "archive-1", "files.archive", arguments)
    store.complete_action(run_id, "archive-1", observation.model_dump())


def pending_approval(erp_db: Path, invoice_id: str) -> None:
    with ledgerlite.connect(erp_db) as conn:
        conn.execute(
            """
            INSERT INTO approvals
                (id, subject_type, subject_id, kind, status, requested_at, note, scenario)
            VALUES ('AP-9001', 'invoice', ?, 'payment', 'pending', '2026-10-01T09:00:00+00:00',
                    'Above the spend limit', 'filed')
            """,
            (invoice_id,),
        )
        conn.commit()


def test_a_verified_run_passes_every_criterion_with_evidence(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.COMPLETED
    results = store.get_verification("RUN-0001")
    assert [result.id for result in results] == [
        "invoice-filed",
        "payment-scheduled",
        "source-archived",
    ]
    assert all(result.ok for result in results)
    assert any(item.startswith("erp:") for item in results[0].evidence)
    assert any(item.startswith("source:") for item in results[0].evidence)
    assert any(item.startswith("payment:") for item in results[1].evidence)
    assert any(item.startswith("file:") for item in results[2].evidence)


def test_skipping_the_erp_write_fails_the_run(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    assert run.error is not None and "invoice-filed" in run.error
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["invoice-filed"].ok
    assert "erp.file_invoice" in results["invoice-filed"].detail
    assert not results["payment-scheduled"].ok
    assert results["source-archived"].ok


def test_a_wrong_amount_in_the_erp_is_caught_against_the_source_document(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice_directly(store, ledgerlite_db, amount_cents=99_900)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["invoice-filed"].ok
    assert "amount" in results["invoice-filed"].detail


def test_an_invoice_filed_without_a_payment_fails_the_payment_criterion(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    file_invoice(store, ledgerlite_db)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["payment-scheduled"].ok


def test_a_pending_approval_satisfies_the_payment_criterion_when_allowed(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])
    pending_approval(ledgerlite_db, invoice_id)

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.COMPLETED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert results["payment-scheduled"].ok
    assert any("approval:" in item for item in results["payment-scheduled"].evidence)


def test_a_filed_invoice_without_an_archived_source_fails(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    schedule_payment(store, ledgerlite_db, invoice_id)

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["source-archived"].ok
    assert "processed" in results["source-archived"].detail


def test_a_payment_that_does_not_cover_the_invoice_fails(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])
    with ledgerlite.connect(ledgerlite_db) as conn:
        conn.execute(
            """
            INSERT INTO payments
                (id, invoice_id, amount_cents, currency, scheduled_for, status, scenario)
            VALUES ('PAY-9001', ?, 10000, 'USD', '2026-10-05', 'scheduled', 'filed')
            """,
            (invoice_id,),
        )
        conn.commit()

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["payment-scheduled"].ok
    assert "covers" in results["payment-scheduled"].detail


def test_a_rejected_invoice_does_not_verify(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, INVOICE["number"])
    with ledgerlite.connect(ledgerlite_db) as conn:
        conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = ?", (invoice_id,))
        conn.commit()

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["invoice-filed"].ok
    assert "rejected" in results["invoice-filed"].detail


def test_a_stale_archive_file_does_not_satisfy_the_archive_criterion(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db)
    schedule_payment(store, ledgerlite_db, invoice_id)
    processed = maildesk_state.shared_root / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "NW-2026-001.pdf").write_bytes(
        (maildesk_state.shared_root / "documents/invoices/NW-2026-001.pdf").read_bytes()
    )

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["source-archived"].ok


def test_an_unknown_check_function_fails_with_a_clear_detail(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    pack = task_pack().model_copy(
        update={
            "verification": task_pack().verification[:1],
        }
    )
    pack.verification[0] = pack.verification[0].model_copy(update={"check": "no_such_check"})

    run = verify_run(
        "RUN-0001",
        pack,
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "no_such_check" in result.detail


def vendor_task_pack() -> TaskPack:
    return load_task_pack(ROOT / "tasks" / "vendor-onboarding.yaml")


def seed_verifying_vendor_run(store: RunStore, run_id: str = "RUN-V001") -> None:
    store.create_run("Onboard Cascade Fabrication LLC", "vendor-onboarding", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)
    store.transition(run_id, RunState.VERIFYING)


VENDOR = {
    "name": "Cascade Fabrication LLC",
    "tax_id": "TAX-2001",
    "email": "accounts@cascade-fabrication.example",
    "address": "4820 Foundry Way, Portland, OR 97210",
}


def create_vendor(
    store: RunStore,
    erp_db: Path,
    *,
    run_id: str = "RUN-V001",
    key: str = "vendor-1",
    **overrides: Any,
) -> str:
    arguments = {**VENDOR, **overrides}
    tools = ToolRegistry(build_erp_tools(MockAccountingSystem(erp_db)), allowlist=["erp.create_vendor"])
    observation = tools.invoke("erp.create_vendor", arguments)
    assert observation.ok, observation.summary
    store.record_action(run_id, key, "erp.create_vendor", arguments)
    store.complete_action(run_id, key, observation.model_dump())
    return observation.data["vendor_id"]


def archive_tax_form(
    store: RunStore,
    shared_root: Path,
    filename: str = "cascade-fabrication-w9.pdf",
    *,
    run_id: str = "RUN-V001",
) -> None:
    tools = ToolRegistry(build_file_tools(shared_root), allowlist=["files.archive"])
    arguments = {"path": f"documents/vendor-onboarding/{filename}", "directory": "archive"}
    observation = tools.invoke("files.archive", arguments)
    assert observation.ok, observation.summary
    store.record_action(run_id, "archive-vendor-1", "files.archive", arguments)
    store.complete_action(run_id, "archive-vendor-1", observation.model_dump())


def test_a_created_vendor_passes_with_evidence(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_vendor_run(store)
    vendor_id = create_vendor(store, ledgerlite_db)
    archive_tax_form(store, maildesk_state.shared_root)

    run = verify_run(
        "RUN-V001",
        vendor_task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.COMPLETED
    results = {result.id: result for result in store.get_verification("RUN-V001")}
    assert {result_id: result.ok for result_id, result in results.items()} == {
        "vendor-created": True,
        "tax-document-archived": True,
    }
    assert f"erp:{vendor_id}" in results["vendor-created"].evidence
    assert any(item.startswith("source:") for item in results["vendor-created"].evidence)
    assert any(item.startswith("file:") for item in results["tax-document-archived"].evidence)


def test_identical_tax_form_copies_are_tolerated(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_vendor_run(store)
    create_vendor(store, ledgerlite_db)
    source = maildesk_state.shared_root / "documents" / "vendor-onboarding" / "cascade-fabrication-w9.pdf"
    shutil.copy(source, maildesk_state.shared_root / "documents" / "cascade-fabrication-w9-copy.pdf")
    archive_tax_form(store, maildesk_state.shared_root)

    run = verify_run(
        "RUN-V001",
        vendor_task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.COMPLETED


def test_a_vendor_created_twice_fails_the_uniqueness_check(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_vendor_run(store)
    vendor_id = create_vendor(store, ledgerlite_db)
    with ledgerlite.connect(ledgerlite_db) as conn:
        conn.execute(
            """
            INSERT INTO vendors (id, name, tax_id, email, status, created_at, scenario)
            VALUES ('V-9001', 'Cascade Fabrication', 'TAX-2001', 'other@cascade.example',
                    'active', '2026-09-06T09:00:00+00:00', 'filed')
            """
        )
        conn.commit()
    archive_tax_form(store, maildesk_state.shared_root)

    run = verify_run(
        "RUN-V001",
        vendor_task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-V001")}
    assert not results["vendor-created"].ok
    assert "expected exactly one" in results["vendor-created"].detail
    assert vendor_id in results["vendor-created"].detail


def test_a_vendor_field_that_disagrees_with_the_tax_form_fails(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_vendor_run(store)
    vendor_id = create_vendor(store, ledgerlite_db)
    with ledgerlite.connect(ledgerlite_db) as conn:
        conn.execute(
            "UPDATE vendors SET email = 'wrong@cascade.example' WHERE id = ?", (vendor_id,)
        )
        conn.commit()
    archive_tax_form(store, maildesk_state.shared_root)

    run = verify_run(
        "RUN-V001",
        vendor_task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-V001")}
    assert not results["vendor-created"].ok
    assert "email" in results["vendor-created"].detail


def test_verifying_a_run_that_is_not_verifying_is_a_no_op(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.CREATED
    assert store.get_verification("RUN-0001") == []


def test_a_scanned_invoice_verifies_against_the_verifiers_own_read(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db, **SCAN)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, SCAN["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
        client=ScriptedClient([vision_fields()]),
    )

    assert run.state is RunState.COMPLETED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert results["invoice-filed"].ok
    assert any(item.startswith("extraction:") for item in results["invoice-filed"].evidence)


def test_a_scanned_invoice_without_a_vision_client_fails(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db, **SCAN)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, SCAN["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["invoice-filed"].ok
    assert "no vision model client" in results["invoice-filed"].detail


def test_a_low_confidence_verifier_read_does_not_verify(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    invoice_id = file_invoice(store, ledgerlite_db, **SCAN)
    schedule_payment(store, ledgerlite_db, invoice_id)
    archive_source(store, maildesk_state.shared_root, SCAN["number"])

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        accounting=MockAccountingSystem(ledgerlite_db),
        shared_root=maildesk_state.shared_root,
        client=ScriptedClient([vision_fields(amount=0.4)]),
    )

    assert run.state is RunState.FAILED
    results = {result.id: result for result in store.get_verification("RUN-0001")}
    assert not results["invoice-filed"].ok
    assert "below the confidence threshold" in results["invoice-filed"].detail


INDIA_PARSED = {
    "supplier_name": SUPPLIER,
    "supplier_gstin": GSTIN,
    "invoice_number": INVOICE_NUMBER,
    "invoice_date": "2026-09-05",
    "total_paise": TOTAL_PAISE,
    "currency": "INR",
    "po_reference": PO_ID,
}


def india_accounting(
    *,
    vendor_tax_id: str = GSTIN,
    vendor_name: str = SUPPLIER,
    po_amount_cents: int = TOTAL_PAISE,
    po_status: str = "open",
    bills: tuple[Bill, ...] = (),
) -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id="V-IN-1001",
                name=vendor_name,
                tax_id=vendor_tax_id,
                email="accounts@papertrail.example",
                address="14 Market Road, Bengaluru",
                status="active",
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                id=PO_ID,
                number=PO_ID,
                vendor_id="V-IN-1001",
                amount_cents=po_amount_cents,
                currency="INR",
                status=po_status,
                issued_at=CREATED_AT,
            )
        ],
        bills=bills,
    )


def matching_pack(**params: Any) -> TaskPack:
    return TaskPack(
        id="zoho-match-test",
        goal_template="Match the extracted invoice against the accounting system",
        sop="india-gst-extraction",
        tools=["invoice.extract"],
        extraction={"confidence_threshold": 0.8},
        verification=[
            VerificationCheck(
                id="accounting-invoice-matches",
                description="The extracted invoice matches the vendor, PO, and is not a duplicate.",
                check="accounting_invoice_matches",
                params=params,
            )
        ],
    )


def seed_extraction(
    store: RunStore,
    *,
    parsed: dict[str, Any] | None = None,
    po_reference: str | None = PO_ID,
    run_id: str = "RUN-0001",
) -> None:
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Extracted the India GST invoice",
            data={
                "parsed": parsed or dict(INDIA_PARSED, po_reference=po_reference),
                "fields": {"po_reference": {"value": po_reference}},
            },
        ),
        tool="invoice.extract",
    )


def test_an_extracted_invoice_matches_the_read_only_accounting_records(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(),
        shared_root=tmp_path,
    )

    assert run.state is RunState.COMPLETED
    result = store.get_verification("RUN-0001")[0]
    assert result.ok
    assert "accounting:vendor:V-IN-1001" in result.evidence
    assert f"accounting:purchaseorder:{PO_ID}" in result.evidence
    assert f"accounting:no-bill:V-IN-1001:{INVOICE_NUMBER}" in result.evidence


def test_a_duplicate_invoice_fails_the_match(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)
    existing = Bill(
        id="INV-Z1",
        number=INVOICE_NUMBER,
        vendor_id="V-IN-1001",
        po_id=PO_ID,
        gr_id=None,
        amount_cents=TOTAL_PAISE,
        currency="INR",
        status="approved",
        received_at=CREATED_AT,
    )

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(bills=(existing,)),
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "INV-Z1" in result.detail
    assert "already" in result.detail
    assert "accounting:bill:INV-Z1" in result.evidence


def test_a_vendor_gstin_mismatch_fails_the_match(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(vendor_tax_id="29AABCV1234C1Z9"),
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "GSTIN" in result.detail


def test_a_void_bill_does_not_block_the_match(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)
    voided = Bill(
        id="INV-Z2",
        number=INVOICE_NUMBER,
        vendor_id="V-IN-1001",
        po_id=PO_ID,
        gr_id=None,
        amount_cents=TOTAL_PAISE,
        currency="INR",
        status="rejected",
        received_at=CREATED_AT,
    )

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(bills=(voided,)),
        shared_root=tmp_path,
    )

    assert run.state is RunState.COMPLETED


def test_a_cancelled_purchase_order_fails_the_match(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(po_status="cancelled"),
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    assert "purchase order" in store.get_verification("RUN-0001")[0].detail


def test_a_purchase_order_amount_mismatch_fails_the_match(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(po_amount_cents=TOTAL_PAISE - 100),
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "purchase order" in result.detail


def test_a_missing_purchase_order_fails_when_the_pack_requires_one(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store, po_reference=None)

    run = verify_run(
        "RUN-0001",
        matching_pack(require_purchase_order=True),
        store,
        accounting=india_accounting(),
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    assert "purchase order" in store.get_verification("RUN-0001")[0].detail


def test_a_missing_purchase_order_is_allowed_by_default(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store, po_reference=None)

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=india_accounting(),
        shared_root=tmp_path,
    )

    assert run.state is RunState.COMPLETED


def india_bill_pack() -> TaskPack:
    return TaskPack(
        id="india-bill-test",
        goal_template="Verify the purchase bill against its source tax invoice",
        sop="india-gst-extraction",
        tools=["erp.file_invoice"],
        verification=[
            VerificationCheck(
                id="bill-filed",
                description="The bill exists once and matches its source.",
                check="india_bill_matches",
            )
        ],
    )


def seed_bill_claim(store: RunStore, bill: Bill) -> None:
    arguments = {
        "number": bill.number,
        "vendor_id": bill.vendor_id,
        "amount": bill.amount_cents / 100,
        "currency": bill.currency,
        "po_id": bill.po_id,
    }
    store.record_action("RUN-0001", "file-1", "erp.file_invoice", arguments)
    store.complete_action(
        "RUN-0001",
        "file-1",
        {
            "ok": True,
            "summary": "Filed the bill",
            "data": {
                "invoice_id": bill.id,
                "number": bill.number,
                "vendor_id": bill.vendor_id,
                "amount_cents": bill.amount_cents,
                "currency": bill.currency,
                "po_id": bill.po_id,
            },
        },
    )


def verified_india_bill(
    tmp_path: Path, *, source_lines: tuple[str, ...] | None = None
) -> tuple[RunStore, InMemoryAccountingSystem]:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    accounting = india_accounting()
    bill = accounting.create_purchase_bill(
        number=INVOICE_NUMBER,
        vendor_id="V-IN-1001",
        amount_cents=TOTAL_PAISE,
        currency="INR",
        po_id=PO_ID,
    )
    seed_bill_claim(store, bill)
    write_invoice_pdf(
        tmp_path / "shared" / "processed" / PDF_FILENAME, lines=source_lines
    )
    return store, accounting


def test_an_india_bill_is_verified_against_its_source_tax_invoice(
    tmp_path: Path,
) -> None:
    store, accounting = verified_india_bill(tmp_path)

    run = verify_run(
        "RUN-0001",
        india_bill_pack(),
        store,
        accounting=accounting,
        shared_root=tmp_path / "shared",
    )

    assert run.state is RunState.COMPLETED
    result = store.get_verification("RUN-0001")[0]
    assert result.ok
    assert any(item.startswith("erp:") for item in result.evidence)
    assert f"source:processed/{PDF_FILENAME}" in result.evidence
    assert f"gstin:{GSTIN}" in result.evidence
    assert "tax:tax_sum" in result.evidence


def test_a_bill_amount_that_differs_from_the_source_fails_the_check(
    tmp_path: Path,
) -> None:
    store, accounting = verified_india_bill(tmp_path)
    original = accounting.list_bills(number=INVOICE_NUMBER)[0]
    # Replace the bill with one whose amount disagrees with the source PDF.
    accounting._bills.clear()
    accounting._bills[original.id] = Bill(
        id=original.id,
        number=original.number,
        vendor_id=original.vendor_id,
        po_id=original.po_id,
        gr_id=None,
        amount_cents=TOTAL_PAISE - 100,
        currency="INR",
        status="received",
        received_at=CREATED_AT,
    )

    run = verify_run(
        "RUN-0001",
        india_bill_pack(),
        store,
        accounting=accounting,
        shared_root=tmp_path / "shared",
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "amount" in result.detail


def test_a_source_with_failing_gst_arithmetic_fails_the_check(
    tmp_path: Path,
) -> None:
    lines = (
        "TAX INVOICE",
        f"Supplier: {SUPPLIER}",
        f"GSTIN: {GSTIN}",
        f"Invoice number: {INVOICE_NUMBER}",
        "Invoice date: 05/09/2026",
        f"Purchase order: {PO_ID}",
        "Taxable amount: 10,000.00",
        "CGST 9%: 900.00",
        "SGST 9%: 900.00",
        "IGST: 0.00",
        "Total: 11,850.00 INR",
    )
    store, accounting = verified_india_bill(tmp_path, source_lines=lines)

    run = verify_run(
        "RUN-0001",
        india_bill_pack(),
        store,
        accounting=accounting,
        shared_root=tmp_path / "shared",
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "invoice total" in result.detail


def test_a_duplicate_match_still_fails_after_the_run_created_its_bill(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    seed_verifying_run(store)
    seed_extraction(store)
    accounting = india_accounting()
    claimed = accounting.create_purchase_bill(
        number=INVOICE_NUMBER,
        vendor_id="V-IN-1001",
        amount_cents=TOTAL_PAISE,
        currency="INR",
        po_id=PO_ID,
    )
    seed_bill_claim(store, claimed)
    other = Bill(
        id="INV-Z9",
        number=INVOICE_NUMBER,
        vendor_id="V-IN-1001",
        po_id=PO_ID,
        gr_id=None,
        amount_cents=TOTAL_PAISE,
        currency="INR",
        status="approved",
        received_at=CREATED_AT,
    )
    accounting._bills[other.id] = other

    run = verify_run(
        "RUN-0001",
        matching_pack(),
        store,
        accounting=accounting,
        shared_root=tmp_path,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "already" in result.detail
