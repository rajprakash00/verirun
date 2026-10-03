"""The Evidence Pack: everything a Run produced, written as runs/<run-id>/evidence.json.

The Report phase writes this file for every Run. It carries the result, the Work
Order, the Plan, the action log, the Verifier's results with evidence references,
the artifacts, the open questions, and the cost and step counters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from company_operator.engine.states import RunState
from company_operator.runs.models import ObservationRecord, Run
from company_operator.runs.store import RunStore

FILE_TOOLS = {"files.write", "files.move", "files.archive"}


def collect_artifacts(observations: list[ObservationRecord]) -> list[str]:
    """Paths the Run produced: screenshot artifacts and files written or moved."""
    artifacts: list[str] = []
    for record in observations:
        artifacts.extend(record.artifacts)
        if record.ok and record.tool in FILE_TOOLS:
            path = record.data.get("path")
            if isinstance(path, str) and path:
                artifacts.append(path)
    return sorted(dict.fromkeys(artifacts))


def build_evidence(run: Run, store: RunStore) -> dict[str, Any]:
    """The Evidence Pack payload for one Run, ready to serialize as JSON."""
    observations = store.list_observations(run.id)
    journal = store.list_journal(run.id)
    verification = store.get_verification(run.id)
    escalations = store.list_escalations(run.id)
    approvals = store.list_approvals(run.id)
    open_questions = list(run.work_order.open_questions) if run.work_order else []
    for escalation in escalations:
        if not escalation.resolved and escalation.question not in open_questions:
            open_questions.append(escalation.question)
    verified = run.state is RunState.COMPLETED
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "request": run.request,
        "state": run.state.value,
        "result": "verified" if verified else run.state.value,
        "verified": verified,
        "work_order": run.work_order.model_dump(mode="json") if run.work_order else None,
        "plan": run.plan.model_dump(mode="json") if run.plan else None,
        "action_log": [
            {
                "id": entry.id,
                "key": entry.key,
                "action": entry.action,
                "payload": entry.payload,
                "status": entry.status,
                "result": entry.result,
                "error": entry.error,
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in journal
        ],
        "observations": [
            {
                "id": record.id,
                "step_position": record.step_position,
                "tool": record.tool,
                "ok": record.ok,
                "summary": record.summary,
                "error_kind": record.error_kind,
                "artifacts": record.artifacts,
                "attempt": record.attempt,
                "created_at": record.created_at.isoformat(),
            }
            for record in observations
        ],
        "escalations": [
            {
                "id": escalation.id,
                "reason": escalation.reason,
                "question": escalation.question,
                "context": escalation.context,
                "resolved": escalation.resolved,
                "created_at": escalation.created_at.isoformat(),
            }
            for escalation in escalations
        ],
        "approvals": [
            {
                "id": request.id,
                "key": request.key,
                "step_id": request.step_id,
                "tool": request.tool,
                "action": request.action,
                "arguments": request.arguments,
                "policy": request.policy,
                "rule": request.rule,
                "reason": request.reason,
                "status": request.status,
                "decision_reason": request.decision_reason,
                "decided_at": request.decided_at.isoformat() if request.decided_at else None,
                "executed_at": request.executed_at.isoformat() if request.executed_at else None,
                "created_at": request.created_at.isoformat(),
            }
            for request in approvals
        ],
        "verification": (
            {
                "passed": all(result.ok for result in verification),
                "checks": [result.model_dump(mode="json") for result in verification],
            }
            if verification
            else None
        ),
        "artifacts": collect_artifacts(observations),
        "open_questions": open_questions,
        "cost_usd": run.cost_usd,
        "steps_used": run.steps_used,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_evidence(run: Run, store: RunStore, directory: str | Path) -> Path:
    """Write the Evidence Pack into a Run's directory and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "evidence.json"
    payload = json.dumps(build_evidence(run, store), indent=2, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")
    return path
