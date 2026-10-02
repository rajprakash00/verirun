"""Start a Run: Resolve, then Plan, with every outcome persisted."""

from __future__ import annotations

from company_operator.context.company import CompanyContext
from company_operator.context.task_pack import TaskPack, TaskPackError
from company_operator.engine.plan import plan_run
from company_operator.engine.resolve import resolve
from company_operator.engine.states import RunState
from company_operator.llm.client import LLMClient
from company_operator.runs.models import Run
from company_operator.runs.store import RunStore


def validate_task_pack(task_pack: TaskPack, context: CompanyContext) -> None:
    if task_pack.sop not in context.sops:
        raise TaskPackError(
            f"Task Pack '{task_pack.id}' references missing SOP '{task_pack.sop}'"
        )
    missing = [policy for policy in task_pack.policies if policy not in context.policies]
    if missing:
        raise TaskPackError(
            f"Task Pack '{task_pack.id}' references missing policies: {missing}"
        )


def start_run(
    request: str,
    task_pack: TaskPack,
    context: CompanyContext,
    client: LLMClient,
    store: RunStore,
    *,
    run_id: str | None = None,
) -> Run:
    validate_task_pack(task_pack, context)
    run = store.create_run(request, task_pack.id, run_id=run_id)
    try:
        store.transition(run.id, RunState.RESOLVING)
        work_order = resolve(request, task_pack, context, client)
        store.save_work_order(run.id, work_order)
        if work_order.open_questions:
            return store.transition(run.id, RunState.NEEDS_HUMAN)
        run_plan = plan_run(work_order, task_pack, client)
        store.save_plan(run.id, run_plan)
        return store.transition(run.id, RunState.PLANNED)
    except Exception as exc:
        store.set_error(run.id, str(exc))
        store.transition(run.id, RunState.FAILED)
        raise
