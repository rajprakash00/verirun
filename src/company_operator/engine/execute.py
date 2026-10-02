"""Execute and Observe: the ReAct tool loop over the Plan, with durable state.

One Step at a time, the model thinks, calls one tool, and observes the result.
Every Observation is stored on the Run with its failure class. Side-effect tools
are journaled under an idempotency key, so a Run that crashes and restarts never
repeats a completed side effect. Checkpoints after each Step mark where to
resume. A Run that reaches its step or cost limit stops in a terminal state.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from company_operator.config import (
    DEFAULT_MAX_COST_USD,
    DEFAULT_MAX_STEPS,
    DEFAULT_PRICES,
    ModelPrice,
)
from company_operator.engine.models import Observation, Step
from company_operator.engine.states import RunState, StepState
from company_operator.llm.client import AssistantTurn, LLMClient, Message, ToolCall
from company_operator.llm.meter import CostMeter
from company_operator.runs.models import Run
from company_operator.runs.store import RunStore
from company_operator.tools.registry import ToolRegistry

EXECUTE_SYSTEM_PROMPT = """\
You are the Execute phase of Operator. You finish one Step of the Plan at a
time by calling tools, one tool call per turn. After every call you observe the
result and decide the next move.

Rules:
- Call only the tools allowed for the current Step, one at a time.
- Read each observation. If a call failed, adapt: try another tool or another
  argument.
- Never repeat a call whose side effect already succeeded.
- When the Step's done criterion is met, reply with a short summary and no tool
  call. That finishes the Step and moves to the next one.
- If the Step cannot be finished, say so plainly in your summary.
"""


class BudgetExceeded(RuntimeError):
    """Raised when a Run reaches its step or cost limit."""


def action_key(step_id: str, tool: str, args: dict[str, Any]) -> str:
    """A stable idempotency key for one side effect inside one Step."""
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    digest = sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{step_id}:{tool}:{digest}"


class RunBudget:
    """The per-run step and cost meters, seeded from the Run's durable usage.

    Every recorded turn and tool call is written straight to the Run, so the
    meters survive a restart and a resumed Run keeps counting toward its limits.
    """

    def __init__(
        self,
        store: RunStore,
        run_id: str,
        *,
        max_steps: int,
        max_cost_usd: float,
        steps_used: int = 0,
        cost_usd: float = 0.0,
        prices: dict[str, ModelPrice] | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self.max_steps = max_steps
        self.max_cost_usd = max_cost_usd
        self.steps_used = steps_used
        self.cost_usd = cost_usd
        self._meter = CostMeter(prices if prices is not None else DEFAULT_PRICES)

    def check(self) -> None:
        if self.steps_used >= self.max_steps:
            raise BudgetExceeded(
                f"step limit reached: {self.steps_used} of {self.max_steps} steps used"
            )
        self.check_cost()

    def check_cost(self) -> None:
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(
                f"cost limit reached: ${self.cost_usd:.4f} of ${self.max_cost_usd:.2f} spent"
            )

    def record_turn(self, turn: AssistantTurn) -> None:
        cost = self._meter.add(turn.model, turn.usage)
        self.cost_usd += cost
        self._store.increment_usage(self._run_id, cost_usd=cost)

    def record_step(self) -> None:
        self.steps_used += 1
        self._store.increment_usage(self._run_id, steps=1)


def execute_run(
    run_id: str,
    client: LLMClient,
    store: RunStore,
    registry: ToolRegistry,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    prices: dict[str, ModelPrice] | None = None,
) -> Run:
    """Execute a planned (or crashed) Run and hand it off to Verify.

    Resuming a Run is the same call: completed Steps are skipped, and journaled
    side effects are never repeated.
    """
    run = store.get_run(run_id)
    if run.plan is None:
        return run
    if run.state in (RunState.PLANNED, RunState.FAILED):
        run = store.transition(run_id, RunState.EXECUTING)
    if run.state is not RunState.EXECUTING:
        return run
    budget = RunBudget(
        store,
        run_id,
        max_steps=max_steps,
        max_cost_usd=max_cost_usd,
        steps_used=run.steps_used,
        cost_usd=run.cost_usd,
        prices=prices,
    )
    return _Executor(run, client, store, registry, budget).execute()


class _Executor:
    def __init__(
        self,
        run: Run,
        client: LLMClient,
        store: RunStore,
        registry: ToolRegistry,
        budget: RunBudget,
    ) -> None:
        self.run = run
        self.run_id = run.id
        self.client = client
        self.store = store
        self.registry = registry
        self.budget = budget

    def execute(self) -> Run:
        plan = self.run.plan
        try:
            completed = self._resume_position()
            step_states = self.store.get_step_states(self.run_id)
            for position, step in enumerate(plan.steps):
                if position <= completed:
                    continue
                if position < len(step_states) and step_states[position] is StepState.DONE:
                    continue
                self._execute_step(position, step, total=len(plan.steps))
            return self.store.transition(self.run_id, RunState.VERIFYING)
        except BudgetExceeded as exc:
            self.store.set_error(self.run_id, str(exc))
            return self.store.transition(self.run_id, RunState.LIMIT_REACHED)
        except Exception as exc:
            self.store.set_error(self.run_id, str(exc))
            self.store.transition(self.run_id, RunState.FAILED)
            raise

    def _resume_position(self) -> int:
        """The position of the last Step completed before a crash, or -1."""
        checkpoint = self.store.latest_checkpoint(self.run_id)
        if checkpoint is None:
            return -1
        position = checkpoint.state.get("step_position")
        return position if isinstance(position, int) else -1

    def _execute_step(self, position: int, step: Step, *, total: int) -> None:
        self.store.set_step_state(self.run_id, position, StepState.RUNNING)
        messages = self._messages(position, step, total)
        tools = self.registry.specs_for(step.allowed_tools)
        while True:
            self.budget.check()
            turn = self.client.complete(messages, tools=tools, model_role="loop")
            self.budget.record_turn(turn)
            self.budget.check_cost()
            if not turn.tool_calls:
                break
            call = turn.tool_calls[0]
            observation = self._call(step, call)
            self.budget.record_step()
            self.store.add_observation(
                self.run_id, observation, step_position=position, tool=call.name
            )
            messages.append(_assistant_message(turn, call))
            messages.append(_tool_message(call.id, observation))
        self.store.set_step_state(self.run_id, position, StepState.DONE)
        self.store.save_checkpoint(
            self.run_id,
            f"step-{position + 1}",
            {"phase": "executing", "step_position": position, "step_id": step.id},
        )

    def _call(self, step: Step, call: ToolCall) -> Observation:
        if call.name not in step.allowed_tools:
            return Observation(
                ok=False,
                summary=(
                    f"Tool '{call.name}' is outside step '{step.id}'; "
                    f"allowed: {', '.join(step.allowed_tools)}"
                ),
                error_kind="policy",
                data={"tool": call.name, "allowed": list(step.allowed_tools)},
            )
        tool = self.registry.get(call.name)
        if tool is None or not tool.side_effect:
            return self.registry.invoke(call.name, call.arguments)
        key = action_key(step.id, call.name, call.arguments)
        entry = self.store.get_journal_entry(self.run_id, key)
        if entry is not None and entry.status == "done" and entry.result is not None:
            return Observation.model_validate(entry.result)
        self.store.record_action(self.run_id, key, call.name, call.arguments)
        observation = self.registry.invoke(call.name, call.arguments)
        if observation.ok:
            self.store.complete_action(self.run_id, key, observation.model_dump())
        else:
            self.store.fail_action(self.run_id, key, observation.summary)
        return observation

    def _messages(self, position: int, step: Step, total: int) -> list[Message]:
        goal = self.run.work_order.goal if self.run.work_order else "(no Work Order)"
        lines = [
            f"Run {self.run_id}",
            f"Work Order goal: {goal}",
            f"Step {position + 1} of {total} ('{step.id}')",
            f"Step goal: {step.goal}",
            f"Done criterion: {step.done_criterion}",
            f"Allowed tools: {', '.join(step.allowed_tools)}",
        ]
        history = [
            observation
            for observation in self.store.list_observations(self.run_id)
            if observation.step_position == position
        ]
        if history:
            lines.append("Observations already made for this Step:")
            lines.extend(
                f"- {item.tool or '(model)'}: "
                f"{'ok' if item.ok else item.error_kind or 'failed'} — {item.summary}"
                for item in history
            )
            lines.append("Do not repeat a side effect listed as ok.")
        return [
            {"role": "system", "content": EXECUTE_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]


def _assistant_message(turn: AssistantTurn, call: ToolCall) -> Message:
    """The executed call only: the loop runs one Tool call per turn."""
    return {
        "role": "assistant",
        "content": turn.text or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
        ],
    }


def _tool_message(call_id: str, observation: Observation) -> Message:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": observation.model_dump_json(),
    }
