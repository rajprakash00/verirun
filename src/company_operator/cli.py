from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from company_operator.config import Settings
from company_operator.context.company import CompanyContextError, load_company_context
from company_operator.context.models import WorkOrder
from company_operator.context.task_pack import TaskPackError, load_task_pack
from company_operator.engine.orchestrator import start_run
from company_operator.engine.states import RunState
from company_operator.llm.client import LLMClient, build_client
from company_operator.runs.models import Run
from company_operator.runs.store import RunStore, generate_run_id


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
        print("report is not implemented yet", file=sys.stderr)
        return 1
    return 0


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
        run = start_run(args.request, task_pack, context, llm, store, run_id=run_id)
    except (CompanyContextError, TaskPackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the CLI reports any engine failure and exits cleanly
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_run(run))
    return 0 if run.state is RunState.PLANNED else 1


def format_run(run: Run) -> str:
    lines = [f"Run {run.id}  state: {run.state.value}", f"Task: {run.task_id}", ""]
    if run.work_order is not None:
        lines.extend(_work_order_lines(run.work_order))
    if run.plan is not None:
        lines.append("Plan")
        for position, step in enumerate(run.plan.steps, start=1):
            lines.append(f"  {position}. {step.id} — {step.goal}")
            lines.append(f"     tools: {', '.join(step.allowed_tools) or '(none)'}")
            lines.append(f"     done: {step.done_criterion}")
        lines.append("")
    if run.error:
        lines.append(f"Error: {run.error}")
    lines.append(f"Run {run.id} stopped in state {run.state.value}.")
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
