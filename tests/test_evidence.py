"""Tests for the Evidence Pack: the Report phase's runs/<run-id>/evidence.json."""

from __future__ import annotations

import json
from pathlib import Path

from company_operator.context.models import WorkOrder
from company_operator.engine.models import CheckResult, Observation, Plan
from company_operator.engine.states import RunState
from company_operator.runs.evidence import build_evidence, write_evidence
from company_operator.runs.store import RunStore
from tests.support import WORK_ORDER, plan_json

PLAN = plan_json()


def seed_completed_run(store: RunStore, run_id: str = "RUN-0001") -> None:
    store.create_run("Process the invoices in the AP mailbox", "invoice-processing", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.save_work_order(run_id, WorkOrder.model_validate(WORK_ORDER))
    store.save_plan(run_id, Plan.model_validate(json.loads(PLAN)))
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Archived the source invoice",
            data={"path": "processed/NW-2026-001.pdf"},
        ),
        step_position=2,
        tool="files.archive",
    )
    store.add_observation(
        run_id,
        Observation(ok=True, summary="Screenshot saved", artifacts=["runs/RUN-0001/shot.png"]),
        step_position=2,
        tool="browser.screenshot",
    )
    store.record_action(run_id, "file-1", "erp.file_invoice", {"number": "NW-2026-001"})
    store.complete_action(run_id, "file-1", {"invoice_id": "INV-3003"})
    store.increment_usage(run_id, steps=3, cost_usd=0.42)
    store.transition(run_id, RunState.VERIFYING)
    store.save_verification(
        run_id,
        [
            CheckResult(
                id="invoice-filed",
                description="The invoice exists in LedgerLite.",
                ok=True,
                detail="INV-3003 agrees with its source",
                evidence=["erp:INV-3003", "source:documents/invoices/NW-2026-001.pdf"],
            )
        ],
    )
    store.transition(run_id, RunState.COMPLETED)


def test_build_evidence_carries_the_whole_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_completed_run(store)
    run = store.get_run("RUN-0001")

    evidence = build_evidence(run, store)

    assert evidence["run_id"] == "RUN-0001"
    assert evidence["result"] == "verified"
    assert evidence["verified"] is True
    assert evidence["work_order"]["sop"] == "invoice-processing"
    assert [step["id"] for step in evidence["plan"]["steps"]] == ["step-1", "step-2", "step-3"]
    assert [entry["action"] for entry in evidence["action_log"]] == ["erp.file_invoice"]
    assert evidence["verification"]["passed"] is True
    assert evidence["verification"]["checks"][0]["id"] == "invoice-filed"
    assert evidence["open_questions"] == []
    assert evidence["cost_usd"] == 0.42
    assert evidence["steps_used"] == 3
    assert set(evidence["artifacts"]) >= {
        "runs/RUN-0001/shot.png",
        "processed/NW-2026-001.pdf",
    }


def test_write_evidence_lands_in_the_run_directory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_completed_run(store)
    run = store.get_run("RUN-0001")

    path = write_evidence(run, store, tmp_path / "runs" / run.id)

    assert path == tmp_path / "runs" / "RUN-0001" / "evidence.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["result"] == "verified"
    assert payload["verification"]["checks"][0]["evidence"] == [
        "erp:INV-3003",
        "source:documents/invoices/NW-2026-001.pdf",
    ]


def test_evidence_for_an_unverified_run_reports_its_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    store.create_run("Process the invoices", "invoice-processing", run_id="RUN-0002")
    run = store.get_run("RUN-0002")

    evidence = build_evidence(run, store)

    assert evidence["result"] == "created"
    assert evidence["verified"] is False
    assert evidence["work_order"] is None
    assert evidence["plan"] is None
    assert evidence["verification"] is None
    assert evidence["artifacts"] == []


def test_evidence_carries_the_escalation_and_its_open_question(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    store.create_run("Process the invoices", "invoice-processing", run_id="RUN-0003")
    store.transition("RUN-0003", RunState.RESOLVING)
    store.escalate(
        "RUN-0003",
        reason="duplicate invoice: AQ-2026-014 already exists as INV-3002",
        question="Should the duplicate AQ-2026-014 be ignored?",
        context={"existing_invoice_id": "INV-3002"},
    )
    run = store.get_run("RUN-0003")

    evidence = build_evidence(run, store)

    assert evidence["result"] == "needs_human"
    assert evidence["open_questions"] == ["Should the duplicate AQ-2026-014 be ignored?"]
    assert len(evidence["escalations"]) == 1
    escalation = evidence["escalations"][0]
    assert escalation["reason"] == "duplicate invoice: AQ-2026-014 already exists as INV-3002"
    assert escalation["context"] == {"existing_invoice_id": "INV-3002"}
    assert escalation["resolved"] is False
