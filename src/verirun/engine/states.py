from __future__ import annotations

from enum import StrEnum


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


class RunState(StrEnum):
    CREATED = "created"
    RESOLVING = "resolving"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    LIMIT_REACHED = "limit_reached"


TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.RESOLVING, RunState.FAILED}),
    RunState.RESOLVING: frozenset({RunState.PLANNED, RunState.NEEDS_HUMAN, RunState.FAILED}),
    RunState.PLANNED: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.EXECUTING,
            RunState.NEEDS_HUMAN,
            RunState.FAILED,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.EXECUTING, RunState.NEEDS_HUMAN, RunState.FAILED}
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.VERIFYING,
            RunState.NEEDS_HUMAN,
            RunState.FAILED,
            RunState.LIMIT_REACHED,
        }
    ),
    RunState.VERIFYING: frozenset({RunState.COMPLETED, RunState.NEEDS_HUMAN, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.LIMIT_REACHED: frozenset(),
    RunState.FAILED: frozenset({RunState.RESOLVING, RunState.EXECUTING}),
    RunState.NEEDS_HUMAN: frozenset({RunState.PLANNED, RunState.EXECUTING}),
}


class InvalidTransitionError(ValueError):
    pass


def can_transition(current: RunState, target: RunState) -> bool:
    return current == target or target in TRANSITIONS[current]


def transition(current: RunState, target: RunState) -> RunState:
    if not can_transition(current, target):
        raise InvalidTransitionError(f"cannot move a Run from {current.value} to {target.value}")
    return target
