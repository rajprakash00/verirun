"""Acceptance tests for Approval Gates: prepare, park, approve, reject.

An irreversible action that policy requires approval for (here, scheduling an
over-limit payment) is prepared as an Approval Request. The Run parks in
``awaiting_approval`` and nothing is submitted until a human decides. Scripted
LLM, real Mock Suite.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.approve import approve, reject
from company_operator.engine.orchestrator import resume_run
from company_operator.engine.states import RunState
from company_operator.runtime import build_registry
from mocks.ledgerlite import db as ledgerlite
from tests.support import FILED_INVOICE_ID, park_over_limit_run


def test_an_over_limit_payment_parks_before_submission(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    _settings, store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )

    run = store.get_run(run_id)
    assert run.state is RunState.AWAITING_APPROVAL
    request = store.open_approval(run_id)
    assert request is not None
    assert request.tool == "erp.schedule_payment"
    assert request.action == "payment.schedule"
    assert request.arguments == {"invoice_id": FILED_INVOICE_ID}
    assert request.policy == "spend-limits"
    assert request.rule == "approval-threshold"
    assert request.step_id == "step-2"
    assert request.status == "pending"

    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'SI-2026-550'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert invoice["amount_cents"] == 1_250_000
    assert payments == []

    journal = [
        entry for entry in store.list_journal(run_id) if entry.action == "erp.schedule_payment"
    ]
    assert [entry.status for entry in journal] == ["failed"]


def test_a_pending_approval_is_never_auto_submitted(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, store, script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")

    run = resume_run(
        run_id,
        task_pack,
        script,
        store,
        build_registry(settings, context, task_pack),
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
        evidence_root=settings.run_db.parent,
    )

    assert run.state is RunState.AWAITING_APPROVAL
    assert store.open_approval(run_id) is not None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        payments = conn.execute(
            """
            SELECT payments.* FROM payments
            JOIN invoices ON invoices.id = payments.invoice_id
            WHERE invoices.number = 'SI-2026-550'
            """
        ).fetchall()
    assert payments == []


def test_approving_resumes_the_run_and_completes_verified(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings, store, script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    request = store.open_approval(run_id)
    assert request is not None
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")

    approve(store, request.id)
    run = resume_run(
        run_id,
        task_pack,
        script,
        store,
        build_registry(settings, context, task_pack),
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
        evidence_root=settings.run_db.parent,
    )

    assert run.state is RunState.COMPLETED
    submitted = store.get_approval(request.id)
    assert submitted.executed_at is not None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'SI-2026-550'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert len(payments) == 1
    assert payments[0]["amount_cents"] == 1_250_000
    assert payments[0]["status"] == "scheduled"

    evidence = json.loads(
        (settings.run_db.parent / run_id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["verified"] is True
    assert evidence["result"] == "verified"


def test_rejecting_aborts_the_run_with_the_recorded_reason(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    _settings, store, _script, run_id = park_over_limit_run(
        tmp_path, ledgerlite_db, maildesk_state
    )
    request = store.open_approval(run_id)
    assert request is not None

    reject(store, request.id, "Budget is frozen until Q4")

    run = store.get_run(run_id)
    assert run.state is RunState.FAILED
    assert run.error is not None
    assert "Budget is frozen until Q4" in run.error
    decided = store.get_approval(request.id)
    assert decided.status == "rejected"
    assert decided.decision_reason == "Budget is frozen until Q4"
    assert decided.decided_at is not None
    assert store.open_approval(run_id) is None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        payments = conn.execute(
            """
            SELECT payments.* FROM payments
            JOIN invoices ON invoices.id = payments.invoice_id
            WHERE invoices.number = 'SI-2026-550'
            """
        ).fetchall()
    assert payments == []
