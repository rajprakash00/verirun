import pytest

from company_operator.engine.states import (
    InvalidTransitionError,
    RunState,
    can_transition,
    transition,
)

ALLOWED = [
    (RunState.CREATED, RunState.RESOLVING),
    (RunState.CREATED, RunState.FAILED),
    (RunState.RESOLVING, RunState.PLANNED),
    (RunState.RESOLVING, RunState.NEEDS_HUMAN),
    (RunState.RESOLVING, RunState.FAILED),
    (RunState.PLANNED, RunState.AWAITING_APPROVAL),
    (RunState.PLANNED, RunState.EXECUTING),
    (RunState.PLANNED, RunState.NEEDS_HUMAN),
    (RunState.PLANNED, RunState.FAILED),
    (RunState.AWAITING_APPROVAL, RunState.EXECUTING),
    (RunState.AWAITING_APPROVAL, RunState.NEEDS_HUMAN),
    (RunState.AWAITING_APPROVAL, RunState.FAILED),
    (RunState.EXECUTING, RunState.AWAITING_APPROVAL),
    (RunState.EXECUTING, RunState.VERIFYING),
    (RunState.EXECUTING, RunState.NEEDS_HUMAN),
    (RunState.EXECUTING, RunState.FAILED),
    (RunState.EXECUTING, RunState.LIMIT_REACHED),
    (RunState.VERIFYING, RunState.COMPLETED),
    (RunState.VERIFYING, RunState.NEEDS_HUMAN),
    (RunState.VERIFYING, RunState.FAILED),
    (RunState.NEEDS_HUMAN, RunState.PLANNED),
    (RunState.NEEDS_HUMAN, RunState.EXECUTING),
    (RunState.FAILED, RunState.RESOLVING),
    (RunState.FAILED, RunState.EXECUTING),
]

REJECTED = [
    (RunState.CREATED, RunState.PLANNED),
    (RunState.CREATED, RunState.EXECUTING),
    (RunState.RESOLVING, RunState.EXECUTING),
    (RunState.PLANNED, RunState.VERIFYING),
    (RunState.PLANNED, RunState.COMPLETED),
    (RunState.AWAITING_APPROVAL, RunState.COMPLETED),
    (RunState.EXECUTING, RunState.PLANNED),
    (RunState.VERIFYING, RunState.PLANNED),
    (RunState.COMPLETED, RunState.EXECUTING),
    (RunState.COMPLETED, RunState.FAILED),
    (RunState.FAILED, RunState.PLANNED),
    (RunState.NEEDS_HUMAN, RunState.COMPLETED),
    (RunState.LIMIT_REACHED, RunState.EXECUTING),
    (RunState.LIMIT_REACHED, RunState.COMPLETED),
]


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed_transitions(current: RunState, target: RunState) -> None:
    assert can_transition(current, target)
    assert transition(current, target) is target


@pytest.mark.parametrize(("current", "target"), REJECTED)
def test_rejected_transitions(current: RunState, target: RunState) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError) as excinfo:
        transition(current, target)
    assert current.value in str(excinfo.value)
    assert target.value in str(excinfo.value)


@pytest.mark.parametrize("state", list(RunState))
def test_transition_to_same_state_is_a_no_op(state: RunState) -> None:
    assert transition(state, state) is state


def test_terminal_states_have_no_exits() -> None:
    assert not can_transition(RunState.COMPLETED, RunState.FAILED)
