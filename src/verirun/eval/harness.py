"""Run the seeded eval scenarios in replay mode and write a report.

The harness seeds the real Mock Suite per scenario, drives every Run end to
end on the unchanged engine with a replayed LLM client, applies the human
decisions the scenario needs, and asserts the known terminal outcome. Every
scenario is independent: a crash in one becomes a failed ScenarioReport, never
a traceback.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from mocks.ledgerlite.app import create_app as create_ledgerlite_app
from mocks.seed.suite import reset_and_seed_suite
from verirun.config import Settings
from verirun.context.company import CompanyContext, load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.approve import approve
from verirun.engine.orchestrator import resume_run, run_task
from verirun.engine.states import RunState
from verirun.eval.models import (
    ApprovalOutcome,
    EscalationOutcome,
    EvalReport,
    EvalTotals,
    ScenarioReport,
)
from verirun.eval.scenarios import SCENARIOS, Scenario
from verirun.llm.client import LLMClient, ReplayClient
from verirun.runs.models import ObservationRecord, Run
from verirun.runs.store import RunStore
from verirun.runtime import build_registry
from verirun.tools import BrowserSession

LEDGERLITE_PORT = 8002

ClientFactory = Callable[[Settings, Scenario], LLMClient]


def run_eval(
    settings: Settings,
    *,
    scenarios: Sequence[Scenario] = SCENARIOS,
    browser: bool = True,
    client_factory: ClientFactory | None = None,
) -> EvalReport:
    """Run every scenario, seeded and replayed, and report the outcomes."""
    factory = client_factory or _replay_client_factory
    settings.run_db.unlink(missing_ok=True)
    context = load_company_context(settings.company_dir)

    reports: list[ScenarioReport] = []
    runnable: list[Scenario] = []
    for scenario in scenarios:
        if scenario.needs_browser and not browser:
            reports.append(_skipped_report(scenario))
        else:
            runnable.append(scenario)

    needs_serve = any(scenario.needs_browser for scenario in runnable)
    if needs_serve:
        _preflight_chromium(settings)
    with ExitStack() as stack:
        if needs_serve:
            stack.enter_context(serve_ledgerlite(settings.erp_db))
        for scenario in runnable:
            reports.append(_run_scenario(settings, scenario, context, factory))

    by_key = {report.key: report for report in reports}
    ordered = [by_key[scenario.key] for scenario in scenarios]
    totals = _totals(ordered)
    return EvalReport(
        generated_at=datetime.now(UTC),
        fixture_dir=str(settings.fixture_dir),
        browser=browser,
        scenarios=ordered,
        totals=totals,
        passed=totals.failed == 0,
    )


def write_report(report: EvalReport, path: str | Path) -> Path:
    """Write the report as JSON, creating the parent directory if need be."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def format_report(report: EvalReport) -> str:
    """A one-line summary plus one line per scenario, for the terminal."""
    totals = report.totals
    lines = [
        (
            f"Eval ({report.mode}, browser {'on' if report.browser else 'off'}): "
            f"{totals.passed}/{totals.scenarios} passed · {totals.failed} failed · "
            f"{totals.skipped} skipped · {totals.completed} completed · "
            f"{totals.steps} steps · ${totals.cost_usd:.4f}"
        )
    ]
    for scenario in report.scenarios:
        mark = "skip" if scenario.skipped else ("pass" if scenario.passed else "FAIL")
        state = scenario.state.value if scenario.state is not None else "(not run)"
        checks = (
            f"{sum(1 for check in scenario.verification if check.ok)}"
            f"/{len(scenario.verification)} checks"
        )
        lines.append(
            f"  [{mark}] {scenario.key}: {state} · {checks} · "
            f"{scenario.human_interventions} human · {scenario.steps} steps · "
            f"${scenario.cost_usd:.4f}"
        )
        for problem in scenario.problems:
            lines.append(f"         problem: {problem}")
    return "\n".join(lines)


def _replay_client_factory(settings: Settings, scenario: Scenario) -> LLMClient:
    return ReplayClient(settings, "replay")


def _skipped_report(scenario: Scenario) -> ScenarioReport:
    return ScenarioReport(
        key=scenario.key,
        title=scenario.title,
        run_id=scenario.run_id,
        expected_state=scenario.expected_state,
        skipped=True,
    )


def _preflight_chromium(settings: Settings) -> None:
    """Prove Chromium starts before the first browser scenario needs it."""
    session = BrowserSession(artifact_dir=settings.run_db.parent / "chromium-check")
    try:
        session.start()
    except Exception as exc:
        raise RuntimeError(
            f"headless Chromium is unavailable ({exc}); "
            "install it with `uv run playwright install chromium`"
        ) from exc
    finally:
        session.close()


def _require_free_port(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"port {port} is already in use; the eval harness serves LedgerLite there"
            ) from exc


@contextmanager
def serve_ledgerlite(db_path: Path, port: int = LEDGERLITE_PORT) -> Iterator[str]:
    """Serve the seeded LedgerLite UI on its canonical port, for the browser."""
    _require_free_port(port)
    server = uvicorn.Server(
        uvicorn.Config(
            create_ledgerlite_app(db_path), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError(f"LedgerLite did not start on {base_url}")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _run_scenario(
    settings: Settings,
    scenario: Scenario,
    context: CompanyContext,
    client_factory: ClientFactory,
) -> ScenarioReport:
    report = ScenarioReport(
        key=scenario.key,
        title=scenario.title,
        run_id=scenario.run_id,
        expected_state=scenario.expected_state,
        parked_state=scenario.parked_state,
    )
    try:
        _drive_scenario(settings, scenario, context, client_factory, report)
    except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failed report
        report.problems.append(f"scenario crashed: {type(exc).__name__}: {exc}")
    report.passed = not report.skipped and not report.problems
    return report


def _drive_scenario(
    settings: Settings,
    scenario: Scenario,
    context: CompanyContext,
    client_factory: ClientFactory,
    report: ScenarioReport,
) -> None:
    task_pack = load_task_pack(settings.tasks_dir / f"{scenario.task_id}.yaml")
    reset_and_seed_suite(settings.erp_db, settings.mail_db, settings.shared_dir)
    client = client_factory(settings, scenario)
    store = RunStore(settings.run_db)
    session = None
    if scenario.needs_browser:
        session = BrowserSession(
            artifact_dir=settings.run_db.parent / scenario.run_id,
            client=client,
            prices=settings.prices,
        )
    try:
        registry = build_registry(settings, context, task_pack, client, browser=session)
        run = run_task(
            scenario.request,
            task_pack,
            context,
            client,
            store,
            registry,
            erp_db_path=settings.erp_db,
            shared_root=settings.shared_dir,
            evidence_root=settings.run_db.parent,
            run_id=scenario.run_id,
        )
        parked = run if scenario.parked_state is not None else None
        if scenario.approval and run.state is RunState.AWAITING_APPROVAL:
            request = store.open_approval(run.id)
            if request is None:
                report.problems.append("the Run parked with no open Approval Request")
            else:
                approve(store, request.id)
                run = resume_run(
                    run.id,
                    task_pack,
                    client,
                    store,
                    registry,
                    erp_db_path=settings.erp_db,
                    shared_root=settings.shared_dir,
                    evidence_root=settings.run_db.parent,
                )
                report.resumed = True
    finally:
        if session is not None:
            session.close()

    _collect(report, scenario, store, run, parked)


def _collect(
    report: ScenarioReport,
    scenario: Scenario,
    store: RunStore,
    run: Run,
    parked: Run | None,
) -> None:
    report.state = run.state
    report.parked_state = parked.state if parked is not None else None
    report.steps = run.steps_used
    report.cost_usd = run.cost_usd
    report.verification = store.get_verification(run.id)
    report.verification_passed = bool(report.verification) and all(
        check.ok for check in report.verification
    )
    report.approvals = [
        ApprovalOutcome(
            id=request.id,
            tool=request.tool,
            action=request.action,
            status=request.status,
            executed=request.executed_at is not None,
        )
        for request in store.list_approvals(run.id)
    ]
    report.escalations = [
        EscalationOutcome(id=item.id, reason=item.reason, question=item.question)
        for item in store.list_escalations(run.id)
    ]
    report.human_interventions = len(report.approvals) + len(report.escalations)
    observations = store.list_observations(run.id)
    report.regrounded_actions = sum(
        1 for item in observations if item.data.get("regrounded") is True
    )
    report.resume_correct = (
        run.state is scenario.expected_state if report.resumed else None
    )
    report.problems.extend(_expectation_problems(report, scenario, observations))


def _expectation_problems(
    report: ScenarioReport,
    scenario: Scenario,
    observations: list[ObservationRecord],
) -> list[str]:
    problems: list[str] = []
    if scenario.parked_state is not None and report.parked_state is not scenario.parked_state:
        got = report.parked_state.value if report.parked_state else "none"
        problems.append(
            f"parked in {got}, expected {scenario.parked_state.value}"
        )
    if report.state is not scenario.expected_state:
        got = report.state.value if report.state is not None else "none"
        problems.append(f"ended in {got}, expected {scenario.expected_state.value}")
    actual_checks = {check.id for check in report.verification}
    if actual_checks != set(scenario.checks):
        problems.append(
            f"verification checks {sorted(actual_checks)}, expected {sorted(scenario.checks)}"
        )
    for check in report.verification:
        if not check.ok:
            problems.append(f"verification check '{check.id}' failed: {check.detail}")
    if len(report.escalations) != scenario.escalations:
        problems.append(
            f"{len(report.escalations)} escalation(s), expected {scenario.escalations}"
        )
    if scenario.escalation_reason is not None and not any(
        scenario.escalation_reason in item.reason for item in report.escalations
    ):
        problems.append(
            f"no escalation reason contains {scenario.escalation_reason!r}"
        )
    if len(report.approvals) != scenario.approvals:
        problems.append(
            f"{len(report.approvals)} approval(s), expected {scenario.approvals}"
        )
    for request in report.approvals:
        if request.status != "approved":
            problems.append(
                f"approval {request.id} is {request.status}, expected approved"
            )
        elif not request.executed:
            problems.append(f"approval {request.id} was approved but never executed")
    if report.resumed != scenario.resumed:
        problems.append(f"resumed={report.resumed}, expected {scenario.resumed}")
    if scenario.regrounded and report.regrounded_actions == 0:
        problems.append("no browser action re-grounded")
    if scenario.retried_tool is not None:
        attempts = [
            item for item in observations if item.tool == scenario.retried_tool
        ]
        first_failed = any(item.attempt == 1 and not item.ok for item in attempts)
        later_ok = any(item.ok and item.attempt > 1 for item in attempts)
        if not (first_failed and later_ok):
            problems.append(
                f"{scenario.retried_tool} was not retried after a failed first attempt"
            )
    return problems


def _totals(reports: Sequence[ScenarioReport]) -> EvalTotals:
    return EvalTotals(
        scenarios=len(reports),
        passed=sum(1 for report in reports if report.passed),
        failed=sum(
            1 for report in reports if not report.passed and not report.skipped
        ),
        skipped=sum(1 for report in reports if report.skipped),
        completed=sum(1 for report in reports if report.state is RunState.COMPLETED),
        steps=sum(report.steps for report in reports),
        cost_usd=sum(report.cost_usd for report in reports),
    )
