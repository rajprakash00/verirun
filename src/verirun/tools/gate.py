"""The policy gate: evaluate each tool's policy action before it runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from verirun.context.policies import PolicyDecision, PolicySet
from verirun.engine.models import IDEMPOTENCY_KEY_ARG
from verirun.tools.base import Tool

RunFacts = Callable[[dict[str, Any]], dict[str, Any]]

# A deterministic safety invariant, not a configurable company policy: a
# purchase bill must never be created from an extraction whose GSTIN or tax
# validations failed. The supplier has to issue a corrected invoice; a human
# approval cannot make invalid tax arithmetic safe.
FAILED_VALIDATION_RULE = "no-bill-for-failed-gst-validation"
MISSING_PO_RULE = "missing-purchase-order-needs-approval"


def _missing_po_approval(facts: dict[str, Any]) -> PolicyDecision | None:
    """Surface the missing-PO reason even when another approval rule also matches.

    A no-PO invoice above the amount threshold would otherwise be labelled only
    as an amount gate, hiding the more important fact that the model supplied a
    purchase order the invoice never printed.
    """
    if facts.get("purchase_order_required") is not True:
        return None
    if facts.get("purchase_order_present") is not False:
        return None
    return PolicyDecision(
        outcome="require_approval",
        policy="india-ap",
        rule=MISSING_PO_RULE,
        reason=(
            "When the Task Pack requires a purchase order, a bill without one "
            "must be approved by a human before it is created."
        ),
    )


def _failed_validation_denial(
    tool: Tool, facts: dict[str, Any]
) -> PolicyDecision | None:
    if tool.action != "bill.create":
        return None
    if facts.get("extraction_validations_failed") is not True:
        return None
    return PolicyDecision(
        outcome="forbid",
        policy="india-ap",
        rule=FAILED_VALIDATION_RULE,
        reason=(
            "A bill whose extraction failed a GSTIN or tax validation is "
            "forbidden. The supplier must issue a corrected invoice."
        ),
    )


class PolicyGate:
    """Evaluates a Tool's declared policy action against Company policies.

    ``run_facts`` adds facts the engine already recorded for this Run, such as
    the extraction confidence behind a proposed bill. Run facts win over Tool
    facts for the same key: they come from the Run's own observations, not from
    the model's proposed arguments.
    """

    def __init__(
        self,
        policies: PolicySet,
        *,
        approval_gates: bool = True,
        run_facts: RunFacts | None = None,
        policy_ids: list[str] | None = None,
    ) -> None:
        self._policies = policies.subset(policy_ids) if policy_ids is not None else policies
        self._approval_gates = approval_gates
        self._run_facts = run_facts

    def check(self, action: str, facts: dict[str, Any] | None = None) -> PolicyDecision:
        return self._policies.evaluate(action, facts or {})

    def check_tool(self, tool: Tool, args: dict[str, Any]) -> PolicyDecision:
        if tool.action is None:
            return PolicyDecision(
                outcome="allow",
                reason=f"{tool.name} has no policy action",
            )
        facts = self._run_facts(args) if self._run_facts is not None else {}
        all_facts = {**tool.policy_facts(args), **facts}
        decision = _failed_validation_denial(tool, all_facts)
        if decision is None:
            decision = self.check(tool.action, all_facts)
            if decision.outcome != "forbid":
                decision = _missing_po_approval(all_facts) or decision
        if not self._approval_gates and decision.outcome == "require_approval":
            return PolicyDecision(
                outcome="allow",
                policy=decision.policy,
                rule=decision.rule,
                reason=f"approval gates disabled (eval ablation): {decision.reason}",
            )
        if decision.outcome == "require_approval":
            # The engine's idempotency key is for the adapter, not for the
            # action a human approves.
            visible = {
                key: value
                for key, value in args.items()
                if key != IDEMPOTENCY_KEY_ARG
            }
            prepared = tool.approval_arguments(visible, all_facts)
            if prepared != visible:
                decision = decision.model_copy(update={"prepared_arguments": prepared})
        return decision
