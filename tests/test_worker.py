"""The worker claims queued Runs, leases them, and records the outcome."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from verirun.adapters import InMemoryAccountingSystem, MockMailSource, Vendor
from verirun.config import Settings
from verirun.engine.models import Plan, Step
from verirun.engine.states import RunState
from verirun.runtime import Backends
from verirun.worker import Worker, enqueue_start

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
RUN_ID = "RUN-WORKER-0001"


def accounting() -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id="V-1001",
                name="Northwind Traders",
                tax_id="TAX-1001",
                email="billing@northwind.example",
                address="1 North Way",
                status="active",
                created_at="2026-09-01T09:00:00+00:00",
            )
        ]
    )

TASK_PACK = """\
id: worker-smoke
title: Worker smoke
goal_template: >-
  File one invoice and finish. Request: {request}
sop: invoice-processing
tools:
  - erp.file_invoice
verification: []
"""


def settings(tmp_path: Path) -> Settings:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "worker-smoke.yaml").write_text(TASK_PACK, encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=tasks,
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=shared,
    )


def script() -> ScriptedClient:
    plan = Plan(
        steps=[
            Step(
                id="step-1",
                goal="File the invoice",
                allowed_tools=["erp.file_invoice"],
                done_criterion="The bill exists",
            )
        ]
    )
    work_order = {
        "sop": "invoice-processing",
        "goal": "File the Northwind invoice",
        "assumptions": [],
        "systems": ["ledgerlite"],
        "policies": [],
        "approval_gates": [],
        "success_criteria": ["One bill exists"],
        "open_questions": [],
    }
    return ScriptedClient(
        [
            json.dumps(work_order),
            plan.model_dump_json(),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "NW-2026-001",
                    "vendor_id": "V-1001",
                    "amount": 1250.00,
                },
                call_id="c1",
            ),
            text_turn("Filed the invoice."),
        ]
    )


def test_a_queued_start_job_is_claimed_and_completed(tmp_path: Path) -> None:
    chosen = settings(tmp_path)
    store, run_id = enqueue_start(
        chosen, "File one invoice", "worker-smoke", run_id=RUN_ID
    )
    books = accounting()
    backends = Backends(
        mail=MockMailSource(chosen.mail_db, chosen.shared_dir), accounting=books
    )
    worker = Worker(
        chosen,
        store=store,
        worker_id="worker-a",
        client_factory=lambda _run_id: script(),
        backends_factory=lambda _settings: backends,
        clock=lambda: datetime.now(UTC),
    )

    item = worker.run_once()

    assert item is not None
    assert item.status == "done", item.last_error
    assert store.get_run(run_id).state is RunState.COMPLETED
    assert store.claim("worker-b", now=T0) is None
    assert worker.run_once() is None


def test_a_worker_failure_is_requeued_and_then_dead_lettered(tmp_path: Path) -> None:
    chosen = settings(tmp_path)
    store, _run_id = enqueue_start(
        chosen, "File one invoice", "worker-smoke", run_id=RUN_ID
    )
    now = {"value": datetime.now(UTC)}

    def broken_client(_run_id: str):
        raise RuntimeError("the model gateway is unavailable")

    worker = Worker(
        chosen,
        store=store,
        worker_id="worker-a",
        client_factory=broken_client,
        clock=lambda: now["value"],
    )

    first = worker.run_once()
    assert first is not None
    assert first.status == "queued"
    assert first.attempts == 1
    assert "model gateway" in (first.last_error or "")

    now["value"] = now["value"] + timedelta(minutes=5)
    second = worker.run_once()
    assert second is not None and second.status == "queued" and second.attempts == 2

    now["value"] = now["value"] + timedelta(minutes=5)
    third = worker.run_once()
    assert third is not None and third.status == "dead" and third.attempts == 3
    assert [item.id for item in store.dead_letter_queue()] == [third.id]
    assert worker.run_once() is None


def test_a_start_job_can_be_retried_without_creating_a_second_run(tmp_path: Path) -> None:
    from verirun.context.company import load_company_context
    from verirun.context.task_pack import load_task_pack
    from verirun.engine.orchestrator import start_run
    from verirun.runs.store import RunStore

    chosen = settings(tmp_path)
    context = load_company_context(chosen.company_dir)
    pack = load_task_pack(chosen.tasks_dir / "worker-smoke.yaml")
    store = RunStore(chosen.run_db)

    first = start_run(
        "File one invoice",
        pack,
        context,
        ScriptedClient([json.dumps({
            "sop": "invoice-processing",
            "goal": "File the invoice",
            "systems": ["ledgerlite"],
            "policies": [],
            "success_criteria": ["One bill exists"],
        }), json.dumps({"steps": [
            {
                "id": "step-1",
                "goal": "File",
                "allowed_tools": ["erp.file_invoice"],
                "done_criterion": "Filed",
            }
        ]})]),
        store,
        run_id="RUN-RETRY-1",
    )
    resumed = start_run(
        "File one invoice", pack, context, ScriptedClient([]), store, run_id="RUN-RETRY-1"
    )

    assert first.state is RunState.PLANNED
    assert resumed.state is RunState.PLANNED
    assert [run.id for run in store.list_runs()] == ["RUN-RETRY-1"]


def test_a_resume_job_runs_after_an_approval_decision(tmp_path: Path) -> None:
    chosen = settings(tmp_path)
    store, run_id = enqueue_start(
        chosen, "File one invoice", "worker-smoke", run_id=RUN_ID
    )
    books = accounting()
    backends = Backends(
        mail=MockMailSource(chosen.mail_db, chosen.shared_dir), accounting=books
    )
    worker = Worker(
        chosen,
        store=store,
        worker_id="worker-a",
        client_factory=lambda _run_id: script(),
        backends_factory=lambda _settings: backends,
        clock=lambda: datetime.now(UTC),
    )
    assert worker.run_once() is not None
    assert store.get_run(run_id).state is RunState.COMPLETED
    # A resume after a human decision is a new job, not a duplicate start.
    store.enqueue(run_id, "resume")
    resumed = worker.run_once()
    assert resumed is not None and resumed.status == "done"
