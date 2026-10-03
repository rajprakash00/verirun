from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from company_operator.config import Settings
from company_operator.context.company import (
    CompanyContext,
    CompanyContextError,
    load_company_context,
)
from company_operator.context.models import WorkOrder
from company_operator.context.task_pack import TaskPack, TaskPackError, load_task_pack
from company_operator.engine.models import CheckResult
from company_operator.engine.orchestrator import report_run, run_task
from company_operator.engine.states import RunState
from company_operator.llm.client import LLMClient, build_client
from company_operator.runs.models import Escalation, Run
from company_operator.runs.store import RunStore, generate_run_id
from company_operator.tools import (
    PolicyGate,
    ToolRegistry,
    build_erp_tools,
    build_file_tools,
    build_mail_tools,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operator",
        description="Turn a company request into completed work.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start a Run from a Request")
    run.add_argument("request", help="the short request, for example: process the invoices")
    run.add_argument("--task", default=None, help="Task Pack id, for example: invoice-processing")

    subparsers.add_parser("serve", help="serve the local dashboard")

    report = subparsers.add_parser("report", help="render the Evidence Pack for a Run")
    report.add_argument("run_id", help="Run id")

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    client: LLMClient | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return _run_command(args, settings, client)
    if args.command == "serve":
        print("serve is not implemented yet", file=sys.stderr)
        return 1
    if args.command == "report":
        return _report_command(args, settings)
    return 0


def build_registry(
    settings: Settings, context: CompanyContext, task_pack: TaskPack
) -> ToolRegistry:
    """Every tool the Task Pack allows, governed by the policy gate."""
    tools = [
        *build_file_tools(settings.shared_dir),
        *build_mail_tools(settings.mail_db, settings.shared_dir),
        *build_erp_tools(settings.erp_db),
    ]
    return ToolRegistry.from_task_pack(tools, task_pack, gate=PolicyGate(context.policies))


def _run_command(
    args: argparse.Namespace, settings: Settings | None, client: LLMClient | None
) -> int:
    settings = settings or Settings()
    if args.task is None:
        print(
            "error: --task is required, for example: operator run \"...\" --task invoice-processing",
            file=sys.stderr,
        )
        return 2
    try:
        context = load_company_context(settings.company_dir)
        task_pack = load_task_pack(settings.tasks_dir / f"{args.task}.yaml")
        run_id = generate_run_id()
        llm = client or build_client(settings, session_id=run_id)
        store = RunStore(settings.run_db)
        registry = build_registry(settings, context, task_pack)
        run = run_task(
            args.request,
            task_pack,
            context,
            llm,
            store,
            registry,
            erp_db_path=settings.erp_db,
            shared_root=settings.shared_dir,
            evidence_root=settings.run_db.parent,
            max_steps=settings.max_steps,
            max_cost_usd=settings.max_cost_usd,
            prices=settings.prices,
            run_id=run_id,
        )
    except (CompanyContextError, TaskPackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the CLI reports any engine failure and exits cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    evidence_path = settings.run_db.parent / run.id / "evidence.json"
    print(
        format_run(
            run,
            store.get_verification(run.id),
            evidence_path,
            escalation=store.open_escalation(run.id),
        )
    )
    return 0 if run.state is RunState.COMPLETED else 1


def _report_command(args: argparse.Namespace, settings: Settings | None) -> int:
    settings = settings or Settings()
    try:
        store = RunStore(settings.run_db)
        run = store.get_run(args.run_id)
        path = report_run(run.id, store, settings.run_db.parent)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any report failure and exits cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Evidence Pack written to {path}")
    return 0 if run.state is RunState.COMPLETED else 1


def format_run(
    run: Run,
    verification: list[CheckResult] | None = None,
    evidence_path: Path | None = None,
    escalation: Escalation | None = None,
) -> str:
    lines = [f"Run {run.id}  state: {run.state.value}", f"Task: {run.task_id}", ""]
    if run.work_order is not None:
        lines.extend(_work_order_lines(run.work_order))
    if escalation is not None:
        lines.append("Escalation")
        lines.append(f"  Reason: {escalation.reason}")
        lines.append(f"  Question: {escalation.question}")
        lines.append("")
    if run.plan is not None:
        lines.append("Plan")
        for position, step in enumerate(run.plan.steps, start=1):
            lines.append(f"  {position}. {step.id} — {step.goal}")
            lines.append(f"     tools: {', '.join(step.allowed_tools) or '(none)'}")
            lines.append(f"     done: {step.done_criterion}")
        lines.append("")
    if verification:
        lines.append("Verification")
        for result in verification:
            mark = "pass" if result.ok else "FAIL"
            lines.append(f"  [{mark}] {result.id} — {result.detail}")
            if result.evidence:
                lines.append(f"         evidence: {', '.join(result.evidence)}")
        lines.append("")
    if evidence_path is not None:
        lines.append(f"Evidence: {evidence_path}")
        lines.append("")
    if run.error:
        lines.append(f"Error: {run.error}")
    verb = "finished" if run.state is RunState.COMPLETED else "stopped"
    lines.append(f"Run {run.id} {verb} in state {run.state.value}.")
    return "\n".join(lines)


def _work_order_lines(work_order: WorkOrder) -> list[str]:
    lines = [
        "Work Order",
        f"  SOP: {work_order.sop}",
        f"  Goal: {work_order.goal}",
    ]
    lines.extend(_list_section("Assumptions", work_order.assumptions))
    lines.append(f"  Systems: {', '.join(work_order.systems) or '(none)'}")
    lines.append(f"  Policies: {', '.join(work_order.policies) or '(none)'}")
    lines.append("  Approval gates:")
    if work_order.approval_gates:
        for gate in work_order.approval_gates:
            policy = f" [{gate.policy}]" if gate.policy else ""
            lines.append(f"    - {gate.id}{policy}: {gate.description}")
    else:
        lines.append("    (none)")
    lines.extend(_list_section("Success criteria", work_order.success_criteria))
    lines.extend(_list_section("Open questions", work_order.open_questions))
    lines.append("")
    return lines


def _list_section(title: str, items: list[str]) -> list[str]:
    lines = [f"  {title}:"]
    lines.extend(f"    - {item}" for item in items)
    if not items:
        lines.append("    (none)")
    return lines
