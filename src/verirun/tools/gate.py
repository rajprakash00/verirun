"""The policy gate: evaluate each tool's policy action before it runs."""

from __future__ import annotations

from typing import Any

from verirun.context.policies import PolicyDecision, PolicySet
from verirun.tools.base import Tool


class PolicyGate:
    """Evaluates a Tool's declared policy action against Company policies."""

    def __init__(self, policies: PolicySet, *, approval_gates: bool = True) -> None:
        self._policies = policies
        self._approval_gates = approval_gates

    def check(self, action: str, facts: dict[str, Any] | None = None) -> PolicyDecision:
        return self._policies.evaluate(action, facts or {})

    def check_tool(self, tool: Tool, args: dict[str, Any]) -> PolicyDecision:
        if tool.action is None:
            return PolicyDecision(
                outcome="allow",
                reason=f"{tool.name} has no policy action",
            )
        decision = self.check(tool.action, tool.policy_facts(args))
        if not self._approval_gates and decision.outcome == "require_approval":
            return PolicyDecision(
                outcome="allow",
                policy=decision.policy,
                rule=decision.rule,
                reason=f"approval gates disabled (eval ablation): {decision.reason}",
            )
        return decision
