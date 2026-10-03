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


def test_evidence_carries_extracted_fields_and_confidences(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_completed_run(store)
    store.add_observation(
        "RUN-0001",
        Observation(
            ok=True,
            summary="Extracted fields from 'documents/invoices/PP-2026-042.pdf'",
            data={
                "path": "documents/invoices/PP-2026-042.pdf",
                "method": "vision",
                "pages": 1,
                "threshold": 0.8,
                "min_confidence": 0.93,
                "fields": {
                    "invoice_number": {"value": "PP-2026-042", "confidence": 0.99},
                    "amount_cents": {"value": 105_000, "confidence": 0.95},
                },
            },
        ),
        step_position=0,
        tool="files.extract",
    )
    store.add_observation(
        "RUN-0001",
        Observation(
            ok=False,
            summary="the scan has fields below the confidence threshold 0.80: amount (0.42)",
            error_kind="invalid",
            data={
                "path": "documents/invoices/QQ-2026-001.pdf",
                "method": "vision",
                "threshold": 0.8,
                "min_confidence": 0.42,
                "fields": {"amount_cents": {"value": 9000, "confidence": 0.42}},
                "low_confidence": True,
            },
        ),
        step_position=0,
        tool="files.extract",
    )

    evidence = build_evidence(store.get_run("RUN-0001"), store)

    extractions = {item["path"]: item for item in evidence["extractions"]}
    assert set(extractions) == {
        "documents/invoices/PP-2026-042.pdf",
        "documents/invoices/QQ-2026-001.pdf",
    }
    scan = extractions["documents/invoices/PP-2026-042.pdf"]
    assert scan["ok"] is True
    assert scan["method"] == "vision"
    assert scan["min_confidence"] == 0.93
    assert scan["fields"]["amount_cents"] == {"value": 105_000, "confidence": 0.95}
    low = extractions["documents/invoices/QQ-2026-001.pdf"]
    assert low["ok"] is False
    assert low["fields"]["amount_cents"]["confidence"] == 0.42


def test_evidence_carries_approval_requests_and_decisions(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    store.create_run("Process the invoices", "invoice-processing", run_id="RUN-0004")
    store.transition("RUN-0004", RunState.RESOLVING)
    store.save_work_order("RUN-0004", WorkOrder.model_validate(WORK_ORDER))
    store.save_plan("RUN-0004", Plan.model_validate(json.loads(PLAN)))
    store.transition("RUN-0004", RunState.PLANNED)
    store.transition("RUN-0004", RunState.EXECUTING)
    request = store.create_approval(
        "RUN-0004",
        key="step-3:erp.schedule_payment:abcd1234",
        step_position=2,
        step_id="step-3",
        tool="erp.schedule_payment",
        action="payment.schedule",
        arguments={"invoice_id": "INV-3003"},
        policy="spend-limits",
        rule="approval-threshold",
        reason="Any payment above 10,000.00 USD must be held at an approval gate.",
    )
    store.transition("RUN-0004", RunState.AWAITING_APPROVAL)
    store.decide_approval(request.id, approved=True)
    run = store.get_run("RUN-0004")

    evidence = build_evidence(run, store)

    assert evidence["state"] == "awaiting_approval"
    assert len(evidence["approvals"]) == 1
    approval = evidence["approvals"][0]
    assert approval["tool"] == "erp.schedule_payment"
    assert approval["action"] == "payment.schedule"
    assert approval["arguments"] == {"invoice_id": "INV-3003"}
    assert approval["status"] == "approved"
    assert approval["decided_at"] is not None
    assert approval["executed_at"] is None


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
