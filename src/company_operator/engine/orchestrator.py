"""Run the engine: Resolve, Plan, Execute, Verify, and Report.

``start_run`` covers the first two phases. ``run_task`` drives a whole Run to
its verified outcome and always leaves an Evidence Pack behind.
"""

from __future__ import annotations

from pathlib import Path

from company_operator.config import DEFAULT_MAX_COST_USD, DEFAULT_MAX_STEPS, ModelPrice
from company_operator.context.company import CompanyContext
from company_operator.context.task_pack import TaskPack, TaskPackError
from company_operator.engine.execute import execute_run
from company_operator.engine.plan import plan_run
from company_operator.engine.resolve import resolve
from company_operator.engine.states import RunState
from company_operator.engine.verify import CHECKS, verify_run
from company_operator.llm.client import LLMClient
from company_operator.runs.evidence import write_evidence
from company_operator.runs.models import Run
from company_operator.runs.store import RunNotFoundError, RunStore, generate_run_id
from company_operator.tools.registry import ToolRegistry


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
    unknown_checks = [check.check for check in task_pack.verification if check.check not in CHECKS]
    if unknown_checks:
        raise TaskPackError(
            f"Task Pack '{task_pack.id}' references unknown verification checks: "
            f"{unknown_checks}; registered: {sorted(CHECKS)}"
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
            store.escalate(
                run.id,
                reason="resolve: the request cannot be completed from the Company Context",
                question=work_order.open_questions[0],
                context={"open_questions": list(work_order.open_questions)},
            )
            return store.get_run(run.id)
        run_plan = plan_run(work_order, task_pack, client)
        store.save_plan(run.id, run_plan)
        return store.transition(run.id, RunState.PLANNED)
    except Exception as exc:
        store.set_error(run.id, str(exc))
        store.transition(run.id, RunState.FAILED)
        raise


def run_task(
    request: str,
    task_pack: TaskPack,
    context: CompanyContext,
    client: LLMClient,
    store: RunStore,
    registry: ToolRegistry,
    *,
    erp_db_path: str | Path,
    shared_root: str | Path,
    evidence_root: str | Path,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    prices: dict[str, ModelPrice] | None = None,
    run_id: str | None = None,
) -> Run:
    """Drive one Request from Resolve to Report, always writing an Evidence Pack."""
    run_id = run_id or generate_run_id()
    try:
        run = start_run(request, task_pack, context, client, store, run_id=run_id)
        if run.state is RunState.PLANNED:
            run = execute_run(
                run.id,
                client,
                store,
                registry,
                max_steps=max_steps,
                max_cost_usd=max_cost_usd,
                prices=prices,
                task_pack=task_pack,
            )
        if run.state is RunState.VERIFYING:
            run = verify_run(
                run.id,
                task_pack,
                store,
                erp_db_path=erp_db_path,
                shared_root=shared_root,
            )
    finally:
        try:
            report_run(run_id, store, evidence_root)
        except RunNotFoundError:
            pass
    return store.get_run(run_id)


def resume_run(
    run_id: str,
    task_pack: TaskPack,
    client: LLMClient,
    store: RunStore,
    registry: ToolRegistry,
    *,
    erp_db_path: str | Path,
    shared_root: str | Path,
    evidence_root: str | Path,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    prices: dict[str, ModelPrice] | None = None,
) -> Run:
    """Continue a parked or interrupted Run through Execute, Verify, and Report.

    A Run still awaiting a human decision is left exactly where it is: no
    timeout ever submits a prepared action.
    """
    run = store.get_run(run_id)
    if run.state in (
        RunState.PLANNED,
        RunState.EXECUTING,
        RunState.AWAITING_APPROVAL,
        RunState.FAILED,
    ):
        run = execute_run(
            run_id,
            client,
            store,
            registry,
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            prices=prices,
            task_pack=task_pack,
        )
    if run.state is RunState.VERIFYING:
        run = verify_run(
            run_id,
            task_pack,
            store,
            erp_db_path=erp_db_path,
            shared_root=shared_root,
        )
    report_run(run_id, store, evidence_root)
    return store.get_run(run_id)


def report_run(run_id: str, store: RunStore, evidence_root: str | Path) -> Path:
    """Write the Evidence Pack for a Run under its own directory."""
    run = store.get_run(run_id)
    return write_evidence(run, store, Path(evidence_root) / run.id)
