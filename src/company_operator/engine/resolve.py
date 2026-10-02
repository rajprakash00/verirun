"""Resolve: turn a Request plus Company Context into a Work Order."""

from __future__ import annotations

import json

from company_operator.context.company import CompanyContext
from company_operator.context.models import WorkOrder
from company_operator.context.task_pack import TaskPack
from company_operator.engine.structured import complete_structured
from company_operator.llm.client import LLMClient

RESOLVE_SYSTEM_PROMPT = """\
You are the Resolve phase of Operator. You read a short Request together with the
Company Context and a Task Pack, then write a Work Order before any action is
taken. You never act, never call tools, and never invent facts.

Use only the Task Pack and Company Context below. A detail is resolved only when
the SOP, a policy, or a precedent states it. Anything else is an open question.

Return a single JSON object with exactly these keys:
{
  "sop": string,
  "goal": string,
  "assumptions": [string],
  "systems": [string],
  "policies": [string],
  "approval_gates": [{"id": string, "description": string, "policy": string}],
  "success_criteria": [string],
  "open_questions": [string]
}

Rules:
- "sop" must be the Task Pack SOP id exactly as given.
- "systems" may only contain ids from Available systems. Name every system the
  work needs.
- "policies" may only contain ids from Task Pack policies. Cite every policy the
  work must obey, by id.
- "approval_gates" list the Task Pack approval rules that apply, citing the
  rule's policy id.
- "success_criteria" are checkable outcomes drawn from the SOP.
- "open_questions" hold anything needed to proceed that the context does not
  answer. Never assume an amount, vendor, invoice number, or tax id; ask
  instead. If the Request is clear and the context resolves every required
  detail, use [].
- Reply with JSON only: no prose, no markdown.
"""


def resolve(
    request: str,
    task_pack: TaskPack,
    context: CompanyContext,
    client: LLMClient,
) -> WorkOrder:
    return complete_structured(
        client,
        [
            {"role": "system", "content": RESOLVE_SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(request, task_pack, context)},
        ],
        WorkOrder,
        model_role="reason",
        validate=lambda work_order: _validate(work_order, task_pack, context),
    )


def _user_message(request: str, task_pack: TaskPack, context: CompanyContext) -> str:
    sop = context.sop(task_pack.sop)
    systems = [entry.model_dump(exclude_none=True) for entry in context.systems.values()]
    policies = [context.policy(policy_id).model_dump() for policy_id in task_pack.policies]
    approval_rules = [rule.model_dump() for rule in task_pack.approval_rules]
    precedents = [precedent.model_dump() for precedent in context.precedents]
    sections = [
        f"Request:\n{request}",
        (
            f"Task Pack '{task_pack.id}'\n"
            f"Title: {task_pack.title}\n"
            f"Goal template: {task_pack.goal_template}"
        ),
        "Available systems (JSON):\n" + json.dumps(systems, indent=2, ensure_ascii=False),
        "Task Pack policies (JSON):\n" + json.dumps(policies, indent=2, ensure_ascii=False),
        f"SOP '{sop.id}' — {sop.title}:\n{sop.body}",
        "Approval rules (JSON):\n" + json.dumps(approval_rules, indent=2, ensure_ascii=False),
        "Precedents (JSON):\n" + json.dumps(precedents, indent=2, ensure_ascii=False),
    ]
    return "\n\n".join(sections)


def _validate(work_order: WorkOrder, task_pack: TaskPack, context: CompanyContext) -> str | None:
    if work_order.sop != task_pack.sop:
        return f"sop must be '{task_pack.sop}', got '{work_order.sop}'"
    if task_pack.policies and not work_order.policies:
        return f"the Work Order must cite at least one Task Pack policy: {task_pack.policies}"
    unknown_policies = [
        policy for policy in work_order.policies if policy not in task_pack.policies
    ]
    if unknown_policies:
        return (
            f"policies outside the Task Pack: {unknown_policies}; "
            f"allowed policy ids: {task_pack.policies}"
        )
    unknown_systems = [system for system in work_order.systems if system not in context.systems]
    if unknown_systems:
        return (
            f"systems outside the registry: {unknown_systems}; "
            f"allowed system ids: {sorted(context.systems)}"
        )
    approval_rules = {rule.id: rule for rule in task_pack.approval_rules}
    for gate in work_order.approval_gates:
        if gate.id not in approval_rules:
            return (
                f"approval gate '{gate.id}' does not match a Task Pack approval rule: "
                f"{sorted(approval_rules)}"
            )
        if gate.policy is None:
            continue
        if gate.policy not in task_pack.policies:
            return f"approval gate '{gate.id}' cites policy '{gate.policy}' outside the Task Pack"
        if gate.policy not in work_order.policies:
            return (
                f"approval gate '{gate.id}' cites policy '{gate.policy}' "
                "but the Work Order does not list it in policies"
            )
    return None
