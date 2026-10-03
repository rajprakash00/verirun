"""Failure adaptation: the fixed ladder from a failed attempt to a human.

Every failure resolves in this order: retry (transient only), alternate
strategy, re-plan (bounded, with failure context), then escalate. Business
conditions that no retry can change -- a policy denial, a duplicate invoice,
an amount mismatch -- skip the ladder and escalate immediately with the
specific reason a human needs.

The ladder is data here so the Execute loop can apply it uniformly; the
question a Run records is what a human reads in the dashboard and Evidence
Pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from company_operator.engine.models import Observation

MAX_TRANSIENT_RETRIES = 2
MAX_ATTEMPTS_PER_STEP = 3
MAX_REPLANS = 1

ESCALATE_TOOL = "task.escalate"
ESCALATE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ESCALATE_TOOL,
        "description": (
            "Stop the Run and ask a human for help. Use when the Step cannot be "
            "completed safely and no tool can resolve it: a missing document or "
            "reference, an ambiguous instruction, or a condition you cannot fix. "
            "State the reason plainly and ask one specific question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "The blocking condition, stated plainly.",
                },
                "question": {
                    "type": "string",
                    "description": "The one question for the human to answer.",
                },
            },
            "required": ["reason", "question"],
            "additionalProperties": False,
        },
    },
}


class FailureAction(StrEnum):
    RETRY = "retry"
    ALTERNATE = "alternate"
    REPLAN = "replan"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class FailureDecision:
    """What to do about one failed Observation, and why."""

    action: FailureAction
    reason: str
    question: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


def classify_failure(
    observation: Observation,
    *,
    failed_attempts: int,
    transient_attempts: int,
    replans_used: int,
    max_transient_retries: int = MAX_TRANSIENT_RETRIES,
    max_attempts: int = MAX_ATTEMPTS_PER_STEP,
    max_replans: int = MAX_REPLANS,
) -> FailureDecision:
    """Route one failed Observation through the fixed escalation order.

    ``failed_attempts`` counts the failures already spent on this Step, not
    including this observation; transient retries do not consume that budget.
    At most ``max_attempts`` failures are adapted before the engine re-plans or
    escalates.
    """
    terminal = _terminal_failure(observation)
    if terminal is not None:
        return terminal
    if observation.error_kind == "transient" and transient_attempts < max_transient_retries:
        return FailureDecision(
            FailureAction.RETRY,
            reason=observation.summary,
            context=observation.data,
        )
    attempts_used = failed_attempts + 1
    if attempts_used < max_attempts:
        return FailureDecision(
            FailureAction.ALTERNATE,
            reason=observation.summary,
            context=observation.data,
        )
    if replans_used < max_replans:
        return FailureDecision(
            FailureAction.REPLAN,
            reason=f"the Step failed {attempts_used} time(s): {observation.summary}",
            context=observation.data,
        )
    return FailureDecision(
        FailureAction.ESCALATE,
        reason=observation.summary,
        question=(
            f"The Step could not be completed after {attempts_used} attempt(s): "
            f"{observation.summary}. How should the Run proceed?"
        ),
        context=observation.data,
    )


def _terminal_failure(observation: Observation) -> FailureDecision | None:
    """A failure no adaptation can fix; escalate it with its specific reason."""
    data = observation.data
    if observation.error_kind == "policy" and data.get("outcome") == "forbid":
        return FailureDecision(
            FailureAction.ESCALATE,
            reason=f"policy denial: {observation.summary}",
            question=f"{observation.summary} What should the Operator do instead?",
            context=data,
        )
    if data.get("duplicate"):
        return FailureDecision(
            FailureAction.ESCALATE,
            reason=f"duplicate invoice: {observation.summary}",
            question=f"{observation.summary}. Should this invoice be ignored?",
            context=data,
        )
    if data.get("mismatch"):
        return FailureDecision(
            FailureAction.ESCALATE,
            reason=f"amount mismatch: {observation.summary}",
            question=f"{observation.summary}. How should the difference be resolved?",
            context=data,
        )
    if data.get("low_confidence"):
        return FailureDecision(
            FailureAction.ESCALATE,
            reason=f"low-confidence extraction: {observation.summary}",
            question=(
                f"{observation.summary}. Should the scanned document be reviewed by a human?"
            ),
            context=data,
        )
    return None
