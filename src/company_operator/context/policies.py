from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Effect = Literal["forbid", "require_approval"]
Operator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"]


class PolicyEvaluationError(ValueError):
    pass


class Predicate(BaseModel):
    """One condition on a fact the caller supplies when evaluating an action."""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: Operator
    value: Any

    def matches(self, facts: dict[str, Any]) -> bool:
        if self.field not in facts:
            return False
        fact = facts[self.field]
        try:
            if self.op == "eq":
                return bool(fact == self.value)
            if self.op == "ne":
                return bool(fact != self.value)
            if self.op == "gt":
                return bool(fact > self.value)
            if self.op == "gte":
                return bool(fact >= self.value)
            if self.op == "lt":
                return bool(fact < self.value)
            if self.op == "lte":
                return bool(fact <= self.value)
            if self.op == "in":
                return bool(fact in self.value)
            return bool(self.value in fact)
        except TypeError as exc:
            raise PolicyEvaluationError(
                f"cannot evaluate {self.field!r} ({fact!r}) {self.op} {self.value!r}"
            ) from exc


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    effect: Effect
    actions: list[str] = Field(default_factory=list)
    when: list[Predicate] = Field(default_factory=list)

    def matches(self, action: str, facts: dict[str, Any]) -> bool:
        if self.actions and action not in self.actions:
            return False
        return all(predicate.matches(facts) for predicate in self.when)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    rules: list[PolicyRule] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    outcome: Literal["allow", "forbid", "require_approval"]
    policy: str | None = None
    rule: str | None = None
    reason: str


class PolicySet:
    """Evaluates an action against every rule. `forbid` beats `require_approval`."""

    def __init__(self, policies: list[Policy]) -> None:
        self._policies = {policy.id: policy for policy in policies}

    def __getitem__(self, policy_id: str) -> Policy:
        return self._policies[policy_id]

    def __contains__(self, policy_id: object) -> bool:
        return policy_id in self._policies

    def __iter__(self) -> Iterator[Policy]:
        return iter(self._policies.values())

    def ids(self) -> list[str]:
        return list(self._policies)

    def evaluate(self, action: str, facts: dict[str, Any] | None = None) -> PolicyDecision:
        facts = facts or {}
        forbid: tuple[Policy, PolicyRule] | None = None
        approval: tuple[Policy, PolicyRule] | None = None
        for policy in self._policies.values():
            for rule in policy.rules:
                if not rule.matches(action, facts):
                    continue
                if rule.effect == "forbid" and forbid is None:
                    forbid = (policy, rule)
                elif rule.effect == "require_approval" and approval is None:
                    approval = (policy, rule)
        chosen = forbid or approval
        if chosen is None:
            return PolicyDecision(outcome="allow", reason=f"no policy rule constrains {action}")
        policy, rule = chosen
        return PolicyDecision(
            outcome=rule.effect,
            policy=policy.id,
            rule=rule.id,
            reason=rule.description,
        )
