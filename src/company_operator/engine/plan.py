"""Plan: turn a Work Order into an ordered list of Steps."""

from __future__ import annotations

import json

from company_operator.context.models import WorkOrder
from company_operator.context.task_pack import TaskPack
from company_operator.engine.models import Plan
from company_operator.engine.structured import complete_structured
from company_operator.llm.client import LLMClient

PLAN_SYSTEM_PROMPT = """\
You are the Plan phase of Operator. You turn a Work Order into an ordered plan
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
- "id" is a short unique slug, for example "step-1", numbered in order.
- "done_criterion" is a checkable statement that says when the Step is finished.
- Reply with JSON only: no prose, no markdown.
"""


def plan_run(work_order: WorkOrder, task_pack: TaskPack, client: LLMClient) -> Plan:
    return complete_structured(
        client,
        [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _user_message(work_order, task_pack),
            },
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
