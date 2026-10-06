"""The tool registry: the Task Pack allowlist and the policy gate in one path."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from verirun.context.policies import PolicyDecision
from verirun.engine.models import ErrorKind, Observation
from verirun.tools.base import Tool
from verirun.tools.gate import PolicyGate

if TYPE_CHECKING:
    from verirun.context.task_pack import TaskPack


def _failure(name: str, summary: str, kind: ErrorKind, **data: Any) -> Observation:
    return Observation(ok=False, summary=summary, error_kind=kind, data={"tool": name, **data})


def _policy_observation(
    decision: PolicyDecision, tool: Tool, arguments: dict[str, Any]
) -> Observation:
    verb = "forbids" if decision.outcome == "forbid" else "requires approval for"
    return _failure(
        tool.name,
        f"Policy {decision.policy} {verb} {tool.action}: {decision.reason}",
        "policy",
        action=tool.action,
        outcome=decision.outcome,
        approval_required=decision.outcome == "require_approval",
        policy=decision.policy,
        rule=decision.rule,
        reason=decision.reason,
        arguments=arguments,
    )


class ToolRegistry:
    """Every tool call goes through the allowlist, then the policy gate."""

    def __init__(
        self,
        tools: Iterable[Tool],
        allowlist: Iterable[str],
        gate: PolicyGate | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name '{tool.name}'")
            self._tools[tool.name] = tool
        self._allowlist = tuple(dict.fromkeys(allowlist))
        self._gate = gate

    @classmethod
    def from_task_pack(
        cls,
        tools: Iterable[Tool],
        task_pack: TaskPack,
        gate: PolicyGate | None = None,
    ) -> ToolRegistry:
        """Build a registry governed by the Task Pack's tool allowlist."""
        return cls(tools, allowlist=task_pack.tools, gate=gate)

    @property
    def allowlist(self) -> list[str]:
        return list(self._allowlist)

    def available(self) -> list[Tool]:
        return [self._tools[name] for name in self._allowlist if name in self._tools]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        """Function definitions for the LLM: allowlisted and registered only."""
        return [tool.spec() for tool in self.available()]

    def specs_for(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """Function definitions the given Step allows, allowlisted and registered only."""
        wanted = set(names)
        return [tool.spec() for tool in self.available() if tool.name in wanted]

    def invoke(self, name: str, args: dict[str, Any] | None = None) -> Observation:
        arguments = dict(args or {})
        if name not in self._allowlist:
            allowed = ", ".join(self._allowlist) or "(none)"
            return _failure(
                name,
                f"Tool '{name}' is outside the Task Pack allowlist; allowed: {allowed}",
                "policy",
                outcome="forbid",
                allowed=list(self._allowlist),
            )
        tool = self._tools.get(name)
        if tool is None:
            return _failure(name, f"No tool named '{name}' is registered.", "not_found")
        if self._gate is not None:
            try:
                decision = self._gate.check_tool(tool, arguments)
            except Exception as exc:  # noqa: BLE001 - a gate failure must not raise
                return _failure(
                    name, f"policy evaluation for {name} failed: {exc}", "unknown"
                )
            if decision.outcome != "allow":
                return _policy_observation(decision, tool, arguments)
        return tool.invoke(arguments)
