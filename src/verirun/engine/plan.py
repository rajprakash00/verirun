"""Plan: turn a Work Order into an ordered list of Steps."""

from __future__ import annotations

import json

from verirun.context.models import WorkOrder
from verirun.context.task_pack import TaskPack
from verirun.engine.models import Plan
from verirun.engine.structured import complete_structured
from verirun.llm.client import LLMClient

PLAN_SYSTEM_PROMPT = """\
You are the Plan phase of Verirun. You turn a Work Order into an ordered plan
of Steps. You never act and never call tools.

Return a single JSON object:
{
  "steps": [
    {
      "id": string,
      "goal": string,
      "allowed_tools": [string],
      "done_criterion": string
    }
  ]
}

Rules:
- Each Step is one piece of work, ordered from first to last.
- "allowed_tools" may only contain tool names from the Task Pack allowlist. Use
  the smallest set of tools that can finish the Step.
- The plan must cover every success criterion in the Work Order and follow the SOP.
- Approval gates are engine mechanics, not Steps: plan the gated action itself,
  for example scheduling an over-limit payment. Calling it parks the Run for a
  human decision automatically, and the Run resumes with the answer. Never plan
  a Step whose work is waiting for, collecting, or recording a human decision.
- "id" is a short unique slug, for example "step-1", numbered in order.
- "done_criterion" is a checkable statement that says when the Step is finished.
- Reply with JSON only: no prose, no markdown.
"""


def plan_run(
    work_order: WorkOrder,
    task_pack: TaskPack,
    client: LLMClient,
    *,
    failure_context: str | None = None,
) -> Plan:
    user_message = _user_message(work_order, task_pack)
    if failure_context:
        user_message += (
            "\n\nA previous attempt at this Work Order failed. Failure context:\n"
            f"{failure_context}\n"
            "Produce a revised plan that avoids the failure and repeats no side "
            "effect that already succeeded."
        )
    return complete_structured(
        client,
        [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        Plan,
        model_role="reason",
        validate=lambda plan: _validate(plan, task_pack),
    )


def _user_message(work_order: WorkOrder, task_pack: TaskPack) -> str:
    sections = [
        "Work Order (JSON):\n"
        + json.dumps(work_order.model_dump(), indent=2, ensure_ascii=False),
        "Tool allowlist:\n" + json.dumps(task_pack.tools, indent=2, ensure_ascii=False),
        f"SOP id: {work_order.sop}",
        f"Task Pack goal template: {task_pack.goal_template}",
    ]
    return "\n\n".join(sections)


def _validate(plan: Plan, task_pack: TaskPack) -> str | None:
    step_ids = [step.id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        return f"step ids must be unique, got {step_ids}"
    for step in plan.steps:
        unknown_tools = [tool for tool in step.allowed_tools if tool not in task_pack.tools]
        if unknown_tools:
            return (
                f"step '{step.id}' uses tools outside the allowlist: {unknown_tools}; "
                f"allowed tools: {task_pack.tools}"
            )
    return None
