"""The Approval phase: prepare irreversible actions and record human decisions.

When policy requires approval for an irreversible Tool call, the engine does
not run it. It prepares an Approval Request holding the exact Tool and
arguments, parks the Run in ``awaiting_approval``, and waits. Approval releases
the prepared call; rejection aborts the Run with the human's reason. Timeouts
never auto-approve: with no decision, the Run stays parked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from verirun.engine.states import RunState

if TYPE_CHECKING:
    from verirun.runs.models import ApprovalRequest
    from verirun.runs.store import RunStore


def prepare_approval(
    store: RunStore,
    run_id: str,
    *,
    key: str,
    step_position: int,
    step_id: str,
    tool: str,
    action: str,
    arguments: dict[str, Any] | None = None,
    policy: str | None = None,
    rule: str | None = None,
    reason: str = "",
) -> ApprovalRequest:
    """Prepare one irreversible action and park the Run in ``awaiting_approval``."""
    request = store.create_approval(
        run_id,
        key=key,
        step_position=step_position,
        step_id=step_id,
        tool=tool,
        action=action,
        arguments=arguments,
        policy=policy,
        rule=rule,
        reason=reason,
    )
    run = store.get_run(run_id)
    if run.state is RunState.EXECUTING:
        store.transition(run_id, RunState.AWAITING_APPROVAL)
    return request


def approve(store: RunStore, approval_id: int) -> ApprovalRequest:
    """Record a yes. The caller resumes the Run."""
    return store.decide_approval(approval_id, approved=True)


def reject(store: RunStore, approval_id: int, reason: str) -> ApprovalRequest:
    """Record a no and abort the Run with that reason."""
    reason = reason.strip() or "rejected without a reason"
    request = store.decide_approval(approval_id, approved=False, reason=reason)
    run = store.get_run(request.run_id)
    store.set_error(request.run_id, f"approval rejected: {reason}")
    if run.state is RunState.AWAITING_APPROVAL:
        store.transition(request.run_id, RunState.FAILED)
    return request
