"""Execute and Observe: the ReAct tool loop over the Plan, with durable state.

One Step at a time, the model thinks, calls one tool, and observes the result.
Every Observation is stored on the Run with its failure class and attempt
number. Failures resolve in a fixed order: transient failures are retried
automatically, then the model may alternate strategy, then the engine re-plans
once with the failure context, and finally the Run escalates to a human with an
open question. Side-effect tools are journaled under an idempotency key, so a
Run that crashes and restarts never repeats a completed side effect.
Checkpoints after each Step mark where to resume. A Run that reaches its step
or cost limit stops in a terminal state.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from company_operator.config import (
    DEFAULT_MAX_COST_USD,
    DEFAULT_MAX_STEPS,
    DEFAULT_PRICES,
    ModelPrice,
)
from company_operator.engine.adapt import (
    ESCALATE_SPEC,
    ESCALATE_TOOL,
    MAX_ATTEMPTS_PER_STEP,
    MAX_REPLANS,
    MAX_TRANSIENT_RETRIES,
    FailureAction,
    FailureDecision,
    classify_failure,
)
from company_operator.engine.approve import prepare_approval
from company_operator.engine.models import Observation, Step
from company_operator.engine.plan import plan_run
from company_operator.engine.states import RunState, StepState
from company_operator.llm.client import AssistantTurn, LLMClient, Message, ToolCall
from company_operator.llm.meter import CostMeter

if TYPE_CHECKING:
    from company_operator.context.task_pack import TaskPack
    from company_operator.runs.models import ObservationRecord, Run
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
- Approval gates are automatic: when you call a tool the policy gates, the Run
  parks for a human decision and resumes with their answer. Call the gated tool
  to request approval; never try to wait for or collect the decision yourself,
  and never record, write, or invent one.
- When the Step's done criterion is met, reply with a short summary and no tool
  call. That finishes the Step and moves to the next one.
- If the Step cannot be finished, say so plainly in your summary.
"""


MAX_OBSERVATION_DATA_CHARS = 4000


class BudgetExceeded(RuntimeError):
    """Raised when a Run reaches its step or cost limit."""


def action_key(step_id: str, tool: str, args: dict[str, Any]) -> str:
    """A stable idempotency key for one side effect inside one Step."""
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    digest = sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{step_id}:{tool}:{digest}"


def _approval_required(observation: Observation) -> bool:
    """True when the policy gate prepared this action for a human instead of running it."""
    return (
        observation.error_kind == "policy"
        and observation.data.get("approval_required") is True
    )


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

    def record_tool_cost(self, observation: Observation) -> None:
        """Count the cost a tool reports for its own model call, such as vision."""
        cost = observation.data.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
            return
        self.cost_usd += float(cost)
        self._store.increment_usage(self._run_id, cost_usd=float(cost))


class StepOutcome(StrEnum):
    DONE = "done"
    ESCALATED = "escalated"
    REPLAN = "replan"
    PARKED = "parked"


def execute_run(
    run_id: str,
    client: LLMClient,
    store: RunStore,
    registry: ToolRegistry,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    prices: dict[str, ModelPrice] | None = None,
    task_pack: TaskPack | None = None,
    max_transient_retries: int = MAX_TRANSIENT_RETRIES,
    max_attempts_per_step: int = MAX_ATTEMPTS_PER_STEP,
    max_replans: int = MAX_REPLANS,
) -> Run:
    """Execute a planned (or crashed, or escalated) Run and hand it to Verify.

    Resuming a Run is the same call: completed Steps are skipped, and journaled
    side effects are never repeated. Passing the Task Pack enables re-planning;
    without one, a worn-out Step escalates instead.
    """
    run = store.get_run(run_id)
    if run.plan is None:
        return run
    if run.state in (RunState.PLANNED, RunState.FAILED):
        run = store.transition(run_id, RunState.EXECUTING)
    elif run.state is RunState.NEEDS_HUMAN:
        store.resolve_escalations(run_id)
        run = store.transition(run_id, RunState.EXECUTING)
    elif run.state is RunState.AWAITING_APPROVAL:
        if store.open_approval(run_id) is not None:
            return run
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
    replannable = task_pack is not None and run.work_order is not None
    executor = _Executor(
        run,
        client,
        store,
        registry,
        budget,
        task_pack=task_pack,
        max_transient_retries=max_transient_retries,
        max_attempts_per_step=max_attempts_per_step,
        max_replans=max_replans if replannable else 0,
    )
    return executor.execute()


class _Executor:
    def __init__(
        self,
        run: Run,
        client: LLMClient,
        store: RunStore,
        registry: ToolRegistry,
        budget: RunBudget,
        *,
        task_pack: TaskPack | None = None,
        max_transient_retries: int = MAX_TRANSIENT_RETRIES,
        max_attempts_per_step: int = MAX_ATTEMPTS_PER_STEP,
        max_replans: int = MAX_REPLANS,
    ) -> None:
        self.run = run
        self.run_id = run.id
        self.client = client
        self.store = store
        self.registry = registry
        self.budget = budget
        self.task_pack = task_pack
        self.max_transient_retries = max_transient_retries
        self.max_attempts_per_step = max_attempts_per_step
        self.max_replans = max_replans
        self.replans_used = store.count_checkpoints(run.id, prefix="replan-")

    def execute(self) -> Run:
        try:
            return self._execute_plan()
        except BudgetExceeded as exc:
            self.store.set_error(self.run_id, str(exc))
            return self.store.transition(self.run_id, RunState.LIMIT_REACHED)
        except Exception as exc:
            self.store.set_error(self.run_id, str(exc))
            self.store.transition(self.run_id, RunState.FAILED)
            raise

    def _execute_plan(self) -> Run:
        plan = self.run.plan
        completed = self._resume_position()
        position = 0
        while position < len(plan.steps):
            step = plan.steps[position]
            step_states = self.store.get_step_states(self.run_id)
            if position <= completed or (
                position < len(step_states) and step_states[position] is StepState.DONE
            ):
                position += 1
                continue
            outcome = self._execute_step(position, step, total=len(plan.steps))
            if outcome in (StepOutcome.ESCALATED, StepOutcome.PARKED):
                return self.store.get_run(self.run_id)
            if outcome is StepOutcome.REPLAN:
                self.replans_used += 1
                self._apply_replan(position)
                self.run = self.store.get_run(self.run_id)
                plan = self.run.plan
                completed = -1
                position = 0
                continue
            position += 1
        return self.store.transition(self.run_id, RunState.VERIFYING)

    def _resume_position(self) -> int:
        """The position of the last Step completed before a crash, or -1.

        Only checkpoints from completed Steps count. A re-plan checkpoint also
        records the position that failed, but the revised plan must be executed
        from the start.
        """
        checkpoint = self.store.latest_checkpoint(self.run_id)
        if checkpoint is None or checkpoint.state.get("phase") != "executing":
            return -1
        position = checkpoint.state.get("step_position")
        return position if isinstance(position, int) else -1

    def _execute_step(self, position: int, step: Step, *, total: int) -> StepOutcome:
        self.store.set_step_state(self.run_id, position, StepState.RUNNING)
        messages = self._messages(position, step, total)
        tools = [*self.registry.specs_for(step.allowed_tools), ESCALATE_SPEC]
        approved = self._approved_calls(position)
        failed_attempts = 0
        transient_attempts = 0
        while True:
            if approved:
                self.budget.check()
                call = approved.pop(0)
                turn = AssistantTurn(model="engine", tool_calls=[call])
            else:
                self.budget.check()
                turn = self.client.complete(messages, tools=tools, model_role="loop")
                self.budget.record_turn(turn)
                self.budget.check_cost()
                if not turn.tool_calls:
                    break
                call = turn.tool_calls[0]
            if call.name == ESCALATE_TOOL:
                self._model_escalation(position, call)
                return StepOutcome.ESCALATED
            observation = self._call(step, call)
            attempt = 1
            self.budget.record_step()
            self._record(position, call.name, observation, attempt)
            if _approval_required(observation):
                return self._park(position, step, call, observation)
            while not observation.ok:
                decision = self._decision(
                    observation,
                    failed_attempts=failed_attempts,
                    transient_attempts=transient_attempts,
                )
                if decision.action is FailureAction.RETRY:
                    transient_attempts += 1
                    attempt += 1
                    self.budget.check()
                    observation = self._call(step, call)
                    self.budget.record_step()
                    self._record(position, call.name, observation, attempt)
                    continue
                failed_attempts += 1
                if decision.action is FailureAction.REPLAN:
                    return StepOutcome.REPLAN
                if decision.action is FailureAction.ESCALATE:
                    self._escalate(
                        decision,
                        tool=call.name,
                        arguments=call.arguments,
                        step_position=position,
                    )
                    return StepOutcome.ESCALATED
                break
            messages.append(_assistant_message(turn, call))
            messages.append(_tool_message(call.id, observation))
        self.store.set_step_state(self.run_id, position, StepState.DONE)
        self.store.save_checkpoint(
            self.run_id,
            f"step-{position + 1}",
            {"phase": "executing", "step_position": position, "step_id": step.id},
        )
        return StepOutcome.DONE

    def _decision(
        self,
        observation: Observation,
        *,
        failed_attempts: int,
        transient_attempts: int,
    ) -> FailureDecision:
        return classify_failure(
            observation,
            failed_attempts=failed_attempts,
            transient_attempts=transient_attempts,
            replans_used=self.replans_used,
            max_transient_retries=self.max_transient_retries,
            max_attempts=self.max_attempts_per_step,
            max_replans=self.max_replans,
        )

    def _record(
        self, position: int, tool: str, observation: Observation, attempt: int
    ) -> None:
        self.store.add_observation(
            self.run_id,
            observation,
            step_position=position,
            tool=tool,
            attempt=attempt,
        )
        self.budget.record_tool_cost(observation)

    def _model_escalation(self, position: int, call: ToolCall) -> None:
        reason = str(call.arguments.get("reason") or "").strip() or "the model asked for help"
        question = str(call.arguments.get("question") or "").strip() or reason
        self.store.add_observation(
            self.run_id,
            Observation(
                ok=False,
                summary=f"Escalated: {reason}",
                error_kind="ambiguity",
                data={"reason": reason, "question": question},
            ),
            step_position=position,
            tool=ESCALATE_TOOL,
        )
        self.store.escalate(
            self.run_id,
            reason=reason,
            question=question,
            context={"step_position": position, "arguments": call.arguments},
        )

    def _escalate(
        self,
        decision: FailureDecision,
        *,
        tool: str,
        arguments: dict[str, Any],
        step_position: int,
    ) -> None:
        question = decision.question or f"{decision.reason}. How should the Run proceed?"
        self.store.escalate(
            self.run_id,
            reason=decision.reason,
            question=question,
            context={
                **decision.context,
                "tool": tool,
                "arguments": arguments,
                "step_position": step_position,
            },
        )

    def _apply_replan(self, position: int) -> None:
        observations = [
            observation
            for observation in self.store.list_observations(self.run_id)
            if observation.step_position == position
        ]
        failures = [
            f"- {item.tool or '(model)'} [{item.error_kind or 'failed'}] "
            f"attempt {item.attempt}: {item.summary}"
            for item in observations
            if not item.ok
        ]
        completed = [
            f"- {entry.action} {json.dumps(entry.payload, sort_keys=True)} succeeded"
            for entry in self.store.list_journal(self.run_id)
            if entry.status == "done"
        ]
        sections = ["Failed attempts:"]
        sections.extend(failures or ["- (none recorded)"])
        sections.append("Completed side effects (never repeat these):")
        sections.extend(completed or ["- (none)"])
        failure_context = "\n".join(sections)
        plan = plan_run(
            self.run.work_order,
            self.task_pack,
            self.client,
            failure_context=failure_context,
        )
        self.store.save_plan(self.run_id, plan)
        self.store.save_checkpoint(
            self.run_id,
            f"replan-{self.replans_used}",
            {"phase": "replanning", "step_position": position, "failure": failure_context},
        )

    def _approved_calls(self, position: int) -> list[ToolCall]:
        """Prepared actions a human approved and the engine has not submitted."""
        return [
            ToolCall(id=f"approval-{request.id}", name=request.tool, arguments=request.arguments)
            for request in self.store.unsubmitted_approvals(self.run_id, position)
        ]

    def _park(
        self, position: int, step: Step, call: ToolCall, observation: Observation
    ) -> StepOutcome:
        """Prepare an irreversible gated action and wait for a human decision."""
        tool = self.registry.get(call.name)
        if tool is None or not tool.irreversible:
            # A gate on a reversible action cannot be prepared safely.
            self._escalate(
                FailureDecision(
                    FailureAction.ESCALATE,
                    reason=f"the policy gate requires approval for a reversible action: "
                    f"{observation.summary}",
                    question=(
                        f"{observation.summary} The action cannot be prepared for approval. "
                        f"How should the Run proceed?"
                    ),
                    context=observation.data,
                ),
                tool=call.name,
                arguments=call.arguments,
                step_position=position,
            )
            return StepOutcome.ESCALATED
        data = observation.data
        prepare_approval(
            self.store,
            self.run_id,
            key=action_key(step.id, call.name, call.arguments),
            step_position=position,
            step_id=step.id,
            tool=call.name,
            action=str(data.get("action") or tool.action or call.name),
            arguments=call.arguments,
            policy=data.get("policy"),
            rule=data.get("rule"),
            reason=str(data.get("reason") or observation.summary),
        )
        return StepOutcome.PARKED

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
        approved = self.store.approved_approval_for(self.run_id, key)
        self.store.record_action(self.run_id, key, call.name, call.arguments)
        if approved is not None:
            # A human approved this exact action; submit it without re-gating.
            observation = tool.invoke(call.arguments)
            if observation.ok:
                self.store.mark_approval_executed(approved.id)
        else:
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
        observations = self.store.list_observations(self.run_id)
        earlier = [
            item
            for item in observations
            if item.step_position is not None and item.step_position < position
        ]
        history = [item for item in observations if item.step_position == position]
        if earlier:
            lines.append(
                "Results from earlier Steps (use these facts; do not repeat a "
                "completed side effect):"
            )
            lines.extend(_observation_lines(earlier))
        if history:
            lines.append("Observations already made for this Step:")
            lines.extend(_observation_lines(history))
            lines.append("Do not repeat a side effect listed as ok.")
        return [
            {"role": "system", "content": EXECUTE_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]


def _observation_lines(observations: list[ObservationRecord]) -> list[str]:
    lines: list[str] = []
    for item in observations:
        outcome = "ok" if item.ok else item.error_kind or "failed"
        label = item.tool or "(model)"
        if item.step_position is not None:
            label = f"Step {item.step_position + 1} {label}"
        lines.append(f"- {label} [{outcome}]: {item.summary}")
        data = _data_snippet(item.data)
        if data:
            lines.append(f"  data: {data}")
    return lines


def _data_snippet(data: dict[str, Any]) -> str:
    if not data:
        return ""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > MAX_OBSERVATION_DATA_CHARS:
        return text[:MAX_OBSERVATION_DATA_CHARS] + " ... (truncated)"
    return text


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
