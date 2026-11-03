"""The policy gate: evaluate each tool's policy action before it runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from verirun.context.policies import PolicyDecision, PolicySet
from verirun.tools.base import Tool

RunFacts = Callable[[dict[str, Any]], dict[str, Any]]


class PolicyGate:
    """Evaluates a Tool's declared policy action against Company policies.

    ``run_facts`` adds facts the engine already recorded for this Run, such as
    the extraction confidence behind a proposed bill. Tool-specific facts win
    over Run facts for the same key.
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
        decision = self.check(tool.action, {**facts, **tool.policy_facts(args)})
        if not self._approval_gates and decision.outcome == "require_approval":
            return PolicyDecision(
                outcome="allow",
                policy=decision.policy,
                rule=decision.rule,
                reason=f"approval gates disabled (eval ablation): {decision.reason}",
            )
        return decision
