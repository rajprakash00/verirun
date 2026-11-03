"""Crash safety: kill the worker at each side-effect transition, then resume.

The accounting fake and the SQLite store survive the kill. A fresh worker claims
the expired lease and resumes the Run. The journal must either replay a
completed write, reconcile an interrupted one, or perform the write exactly
once. Every scenario ends with exactly one purchase bill.

The unknown-outcome scenario is the one case that must not resume by itself: it
parks with an Exception that carries the vendor, invoice number, and journal key
a human needs to reconcile it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from verirun.adapters import (
    GoodsReceipt,
    InMemoryAccountingSystem,
    MockMailSource,
    PurchaseOrder,
    Vendor,
    WriteOutcomeUnknownError,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.models import WorkOrder
from verirun.context.task_pack import load_task_pack
from verirun.engine.models import Plan, Step
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends
from verirun.worker import Worker

CREATED_AT = "2026-09-01T09:00:00+00:00"
INVOICE_NUMBER = "NW-2026-001"
VENDOR_ID = "V-1001"
RUN_ID = "RUN-CRASH-0001"
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

TASK_PACK = """\
id: crash-safety
title: Crash safety
goal_template: >-
  File one invoice and leave the journal resumable. Request: {request}
sop: invoice-processing
tools:
  - erp.file_invoice
verification: []
"""


class WorkerKilled(BaseException):
    """Stands in for a process kill: nothing may catch it as an ordinary error."""


def accounting() -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id=VENDOR_ID,
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
                vendor_id=VENDOR_ID,
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


def settings(tmp_path: Path, seed_shared: Path) -> Settings:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "crash-safety.yaml").write_text(TASK_PACK, encoding="utf-8")
    shared = tmp_path / "work-shared"
    source = shared / "documents" / "invoices" / f"{INVOICE_NUMBER}.pdf"
    source.parent.mkdir(parents=True)
    shutil.copy2(
        seed_shared / "documents" / "invoices" / f"{INVOICE_NUMBER}.pdf",
        source,
    )
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=tasks,
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=shared,
    )


def planned_run(chosen: Settings) -> str:
    store = RunStore(chosen.run_db)
    store.create_run("File one invoice", "crash-safety", run_id=RUN_ID)
    store.transition(RUN_ID, RunState.RESOLVING)
    store.save_work_order(
        RUN_ID,
        WorkOrder(
            sop="invoice-processing",
            goal="File the Northwind invoice",
            systems=["ledgerlite"],
            policies=[],
            success_criteria=["One bill exists"],
        ),
    )
    store.save_plan(
        RUN_ID,
        Plan(
            steps=[
                Step(
                    id="step-1",
                    goal="File the invoice",
                    allowed_tools=["erp.file_invoice"],
                    done_criterion="The bill exists",
                )
            ]
        ),
    )
    store.transition(RUN_ID, RunState.PLANNED)
    store.enqueue(RUN_ID, "resume", now=T0)
    return RUN_ID


def script() -> ScriptedClient:
    return ScriptedClient(
        [
            tool_turn(
                "erp.file_invoice",
                {
                    "number": INVOICE_NUMBER,
                    "vendor_id": VENDOR_ID,
                    "amount": 1250.00,
                    "po_id": "PO-2001",
                    "gr_id": "GR-2501",
                },
                call_id="c1",
            ),
            text_turn("Filed the invoice."),
        ]
    )


def worker(
    chosen: Settings,
    store: RunStore,
    accounting_system: InMemoryAccountingSystem,
    now,
    *,
    client_factory=None,
):
    backends = Backends(
        mail=MockMailSource(chosen.mail_db, chosen.shared_dir),
        accounting=accounting_system,
    )
    return Worker(
        chosen,
        store=store,
        worker_id=f"worker-{now.timestamp()}",
        lease_seconds=10,
        client_factory=client_factory or (lambda _run_id: script()),
        backends_factory=lambda _settings: backends,
        clock=lambda: now,
    )


@pytest.mark.parametrize(
    "crash_point",
    ["before-intent", "after-intent", "after-call", "after-done"],
)
def test_a_kill_at_any_transition_resumes_without_a_duplicate_bill(
    tmp_path: Path,
    maildesk_state,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    chosen = settings(tmp_path, maildesk_state.shared_root)
    books = accounting()
    store = RunStore(chosen.run_db)
    planned_run(chosen)

    original_record = store.record_action
    original_complete = store.complete_action

    def crash_record(*args, **kwargs):
        if crash_point == "before-intent":
            raise WorkerKilled("killed before the intent was committed")
        result = original_record(*args, **kwargs)
        if crash_point == "after-intent":
            raise WorkerKilled("killed after the intent, before the call")
        return result

    def crash_complete(*args, **kwargs):
        if crash_point == "after-call":
            raise WorkerKilled("killed after the call, before done")
        result = original_complete(*args, **kwargs)
        if crash_point == "after-done":
            raise WorkerKilled("killed after done, before the checkpoint")
        return result

    with monkeypatch.context() as patched:
        patched.setattr(store, "record_action", crash_record)
        patched.setattr(store, "complete_action", crash_complete)
        with pytest.raises(WorkerKilled):
            worker(chosen, store, books, T0).run_once()

    # The lease belongs to the killed worker until it expires.
    assert store.active_queue_item(RUN_ID) is not None
    assert store.get_run(RUN_ID).state in (RunState.PLANNED, RunState.EXECUTING)

    restarted = worker(chosen, store, books, T0 + timedelta(seconds=30))
    item = restarted.run_once()

    assert item is not None
    assert item.status == "done"
    assert store.get_run(RUN_ID).state is RunState.COMPLETED
    assert len(books.list_bills(number=INVOICE_NUMBER)) == 1
    entries = [
        entry
        for entry in store.list_journal(RUN_ID)
        if entry.action == "erp.file_invoice"
    ]
    assert len(entries) == 1
    assert entries[0].status == "done"


def test_resume_reconciles_the_outbox_before_the_model_acts(
    tmp_path: Path, maildesk_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = settings(tmp_path, maildesk_state.shared_root)
    books = accounting()
    store = RunStore(chosen.run_db)
    planned_run(chosen)

    def crash_before_done(*args, **kwargs):
        raise WorkerKilled("killed after the call, before done")

    with monkeypatch.context() as patched:
        patched.setattr(store, "complete_action", crash_before_done)
        with pytest.raises(WorkerKilled):
            worker(chosen, store, books, T0).run_once()

    entries = [
        entry for entry in store.list_journal(RUN_ID) if entry.action == "erp.file_invoice"
    ]
    assert entries[0].status == "intent"

    # The model never repeats the tool call: the engine itself reconciles the
    # outbox entry by its natural key before Execute starts.
    restarted = worker(
        chosen,
        store,
        books,
        T0 + timedelta(seconds=30),
        client_factory=lambda _run_id: ScriptedClient([text_turn("The bill is already filed.")]),
    )
    item = restarted.run_once()

    assert item is not None and item.status == "done"
    entries = [
        entry for entry in store.list_journal(RUN_ID) if entry.action == "erp.file_invoice"
    ]
    assert entries[0].status == "done"
    assert len(books.list_bills(number=INVOICE_NUMBER)) == 1


class UnknownWriteAccounting(InMemoryAccountingSystem):
    """A write that may have landed: every call raises an unknown outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def create_purchase_bill(self, **kwargs):  # type: ignore[override]
        self.calls += 1
        raise WriteOutcomeUnknownError(
            "Zoho Books did not answer the create request",
            {"endpoint": "/bills", "number": kwargs.get("number")},
        )


def test_an_unknown_write_parks_with_an_exception_and_never_retries(
    tmp_path: Path, maildesk_state
) -> None:
    chosen = settings(tmp_path, maildesk_state.shared_root)
    books = UnknownWriteAccounting()
    store = RunStore(chosen.run_db)
    planned_run(chosen)

    item = worker(chosen, store, books, T0).run_once()
    assert item is not None and item.status == "done"

    run = store.get_run(RUN_ID)
    assert run.state is RunState.NEEDS_HUMAN
    exception = store.open_exception(RUN_ID)
    assert exception is not None
    assert exception.kind == "unknown_write_outcome"
    assert exception.evidence["claim_key"] == f"bill:{VENDOR_ID}:{INVOICE_NUMBER.casefold()}"
    assert exception.evidence["claim_detail"]["number"] == INVOICE_NUMBER

    entry = next(
        entry
        for entry in store.list_journal(RUN_ID)
        if entry.action == "erp.file_invoice"
    )
    assert entry.status == "unknown"

    # A human resumes without reconciling: the write is not invoked again.

    from verirun.engine.orchestrator import resume_run
    from verirun.runtime import live_registry

    context = load_company_context(chosen.company_dir)
    pack = load_task_pack(chosen.tasks_dir / "crash-safety.yaml")
    llm = script()
    backends = Backends(
        mail=MockMailSource(chosen.mail_db, chosen.shared_dir), accounting=books
    )
    with live_registry(
        chosen, context, pack, llm, RUN_ID, backends=backends, store=store
    ) as registry:
        run = resume_run(
            RUN_ID,
            pack,
            llm,
            store,
            registry,
            accounting=backends.verifier_accounting,
            shared_root=backends.mail.document_root,
            evidence_root=chosen.run_db.parent,
        )

    assert run.state is RunState.NEEDS_HUMAN
    assert books.calls == 1
    assert store.open_exception(RUN_ID) is not None
    assert len(store.list_exceptions(RUN_ID)) == 1
