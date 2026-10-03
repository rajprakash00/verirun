"""Tests for the Verifier: independent checks against LedgerLite and the filesystem.

Ground truth is seeded by the mocks. The Run's action journal is written the way
the executor writes it, then the Verifier inspects real state only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from company_operator.context.task_pack import TaskPack, load_task_pack
from company_operator.engine.states import RunState
from company_operator.engine.verify import verify_run
from company_operator.runs.store import RunStore
from company_operator.tools import ToolRegistry, build_erp_tools, build_file_tools
from mocks.ledgerlite import db as ledgerlite
from tests.support import ROOT

INVOICE = {
    "number": "NW-2026-001",
    "vendor_id": "V-1001",
    "amount": 1250.00,
    "po_id": "PO-2001",
    "gr_id": "GR-2501",
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
    tools = ToolRegistry(build_erp_tools(erp_db), allowlist=["erp.file_invoice"])
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
    tools = ToolRegistry(build_erp_tools(erp_db), allowlist=["erp.schedule_payment"])
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
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
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.FAILED
    result = store.get_verification("RUN-0001")[0]
    assert not result.ok
    assert "no_such_check" in result.detail


def test_verifying_a_run_that_is_not_verifying_is_a_no_op(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    run = verify_run(
        "RUN-0001",
        task_pack(),
        store,
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
    )

    assert run.state is RunState.CREATED
    assert store.get_verification("RUN-0001") == []
