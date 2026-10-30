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
    AblationCase,
    AblationReport,
    ApprovalOutcome,
    EscalationOutcome,
    EvalReport,
    EvalTotals,
    ScenarioReport,
)
from verirun.eval.scenarios import SCENARIOS, SILENT_FAILURE_SCENARIOS, Scenario
from verirun.llm.client import LLMClient, ReplayClient
from verirun.runs.models import ObservationRecord, Run
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry
from verirun.tools import BrowserSession

LEDGERLITE_PORT = 8002

ClientFactory = Callable[[Settings, Scenario], LLMClient]


def run_eval(
    settings: Settings,
    *,
    scenarios: Sequence[Scenario] = SCENARIOS,
    browser: bool = True,
    client_factory: ClientFactory | None = None,
    verifier: bool = True,
    approval_gates: bool = True,
) -> EvalReport:
    """Run the seeded scenarios, seeded and replayed, and report the outcomes.

    By default the report also contrasts the baseline with two eval-only
    ablations: the Verifier disabled, and the Approval Gates disabled. Passing
    ``verifier=False`` or ``approval_gates=False`` runs just that ablation and
    reports its raw outcomes. Neither switch exists on the product paths.
    """
    factory = client_factory or _replay_client_factory
    is_default_run = tuple(scenarios) == tuple(SCENARIOS)
    scenarios = (*scenarios, *SILENT_FAILURE_SCENARIOS) if is_default_run else scenarios

    if not verifier or not approval_gates:
        return _run_pass(
            settings,
            scenarios=scenarios,
            browser=browser,
            client_factory=factory,
            verifier=verifier,
            approval_gates=approval_gates,
            check_expectations=False,
        )

    baseline = _run_pass(
        settings,
        scenarios=scenarios,
        browser=browser,
        client_factory=factory,
        verifier=True,
        approval_gates=True,
    )
    if not is_default_run:
        return baseline

    verifier_off = _run_pass(
        settings,
        scenarios=scenarios,
        browser=False,
        client_factory=factory,
        verifier=False,
        approval_gates=True,
        check_expectations=False,
        evidence_root=settings.run_db.parent / "ablations" / "verifier-off",
    )
    gates_off = _run_pass(
        settings,
        scenarios=scenarios,
        browser=False,
        client_factory=factory,
        verifier=True,
        approval_gates=False,
        check_expectations=False,
        evidence_root=settings.run_db.parent / "ablations" / "gates-off",
    )
    return _with_ablations(baseline, verifier_off, gates_off)


def _run_pass(
    settings: Settings,
    *,
    scenarios: Sequence[Scenario],
    browser: bool,
    client_factory: ClientFactory,
    verifier: bool,
    approval_gates: bool,
    check_expectations: bool = True,
    evidence_root: Path | None = None,
) -> EvalReport:
    settings.run_db.unlink(missing_ok=True)
    context = load_company_context(settings.company_dir)
    evidence_root = evidence_root or settings.run_db.parent

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
            reports.append(
                _run_scenario(
                    settings,
                    scenario,
                    context,
                    client_factory,
                    verifier=verifier,
                    approval_gates=approval_gates,
                    check_expectations=check_expectations,
                    evidence_root=evidence_root,
                )
            )

    by_key = {report.key: report for report in reports}
    ordered = [by_key[scenario.key] for scenario in scenarios]
    totals = _totals(ordered)
    return EvalReport(
        generated_at=datetime.now(UTC),
        fixture_dir=str(settings.fixture_dir),
        browser=browser,
        verifier=verifier,
        approval_gates=approval_gates,
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
    switches = (
        f"verifier {'on' if report.verifier else 'off'}, "
        f"gates {'on' if report.approval_gates else 'off'}"
    )
    lines = [
        (
            f"Eval ({report.mode}, {switches}, browser "
            f"{'on' if report.browser else 'off'}): "
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
    if report.ablations is not None:
        ablation = report.ablations
        lines.append(
            f"Ablations: verifier caught {ablation.caught_failures}/"
            f"{ablation.probe_failures} failure(s) "
            f"across {ablation.caught_checks} check(s); "
            f"{ablation.undetected_failures} went undetected without it; "
            f"{ablation.gated_irreversible_actions} gated irreversible action(s) "
            f"became {ablation.ungated_irreversible_actions} ungated; "
            f"{ablation.policy_denials} policy denial(s) still enforced."
        )
        for problem in ablation.problems:
            lines.append(f"  [ablation problem] {problem}")
        for case in ablation.cases:
            if case.kind == "verifier" and case.undetected:
                lines.append(
                    f"  [ablation] {case.key}: verifier failed "
                    f"{case.caught_checks} -> {case.verifier_off_state.value} "
                    f"without verifier (undetected)"
                )
            elif case.kind == "approval_gates" and case.ungated_actions:
                lines.append(
                    f"  [ablation] {case.key}: gated {case.gated_actions} -> "
                    f"{case.gates_off_state.value if case.gates_off_state else 'not run'} "
                    f"with {case.ungated_actions} submitted without a gate"
                )
            elif case.kind == "approval_gates" and case.gates_off_escalations:
                lines.append(
                    f"  [ablation] {case.key}: still denied by policy "
                    f"({case.gates_off_escalations} escalation(s))"
                )
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
    *,
    verifier: bool,
    approval_gates: bool,
    check_expectations: bool,
    evidence_root: Path,
) -> ScenarioReport:
    report = ScenarioReport(
        key=scenario.key,
        title=scenario.title,
        run_id=scenario.run_id,
        expected_state=scenario.expected_state,
        parked_state=scenario.parked_state,
    )
    try:
        _drive_scenario(
            settings,
            scenario,
            context,
            client_factory,
            report,
            verifier=verifier,
            approval_gates=approval_gates,
            check_expectations=check_expectations,
            evidence_root=evidence_root,
        )
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
    *,
    verifier: bool,
    approval_gates: bool,
    check_expectations: bool,
    evidence_root: Path,
) -> None:
    task_pack = load_task_pack(settings.tasks_dir / f"{scenario.task_id}.yaml")
    reset_and_seed_suite(settings.erp_db, settings.mail_db, settings.shared_dir)
    client = client_factory(settings, scenario)
    store = RunStore(settings.run_db)
    session = None
    if scenario.needs_browser:
        session = BrowserSession(
            artifact_dir=evidence_root / scenario.run_id,
            client=client,
            prices=settings.prices,
        )
    try:
        backends = Backends.mock_suite(settings)
        registry = build_registry(
            settings,
            context,
            task_pack,
            client,
            backends=backends,
            browser=session,
            approval_gates=approval_gates,
        )
        run = run_task(
            scenario.request,
            task_pack,
            context,
            client,
            store,
            registry,
            accounting=backends.verifier_accounting,
            shared_root=backends.mail.document_root,
            evidence_root=evidence_root,
            run_id=scenario.run_id,
            verifier=verifier,
            write_evidence=verifier,
        )
        parked = run if run.state is scenario.parked_state else None
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
                    accounting=backends.verifier_accounting,
                    shared_root=backends.mail.document_root,
                    evidence_root=evidence_root,
                    verifier=verifier,
                    write_evidence=verifier,
                )
                report.resumed = True
    finally:
        if session is not None:
            session.close()

    _collect(
        report,
        scenario,
        store,
        run,
        parked,
        check_expectations=check_expectations,
    )


def _collect(
    report: ScenarioReport,
    scenario: Scenario,
    store: RunStore,
    run: Run,
    parked: Run | None,
    *,
    check_expectations: bool,
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
    report.actions = [
        entry.action for entry in store.list_journal(run.id) if entry.status == "done"
    ]
    observations = store.list_observations(run.id)
    report.regrounded_actions = sum(
        1 for item in observations if item.data.get("regrounded") is True
    )
    report.resume_correct = (
        run.state is scenario.expected_state if report.resumed else None
    )
    if check_expectations:
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
    failed_checks = {check.id for check in report.verification if not check.ok}
    expected_failed = set(scenario.expected_failed_checks)
    if failed_checks != expected_failed:
        problems.append(
            f"failed verification checks {sorted(failed_checks)}, "
            f"expected {sorted(expected_failed)}"
        )
    for check in report.verification:
        if not check.ok and check.id not in expected_failed:
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


def _with_ablations(
    baseline: EvalReport,
    verifier_off: EvalReport,
    gates_off: EvalReport,
) -> EvalReport:
    """Keep the standard scenarios in the report and attach the contrast."""
    by_key = {report.key: report for report in baseline.scenarios}
    ordered = [by_key[scenario.key] for scenario in SCENARIOS if scenario.key in by_key]
    totals = _totals(ordered)
    report = baseline.model_copy(
        update={
            "scenarios": ordered,
            "totals": totals,
            "passed": totals.failed == 0,
        }
    )
    ablation = _compare(baseline, verifier_off, gates_off)
    report.ablations = ablation
    report.passed = totals.failed == 0 and ablation.passed
    return report


def _compare(
    baseline: EvalReport,
    verifier_off: EvalReport,
    gates_off: EvalReport,
) -> AblationReport:
    base = {report.key: report for report in baseline.scenarios}
    without_verifier = {report.key: report for report in verifier_off.scenarios}
    without_gates = {report.key: report for report in gates_off.scenarios}

    cases: list[AblationCase] = []
    problems: list[str] = []
    caught_failures = 0
    caught_checks = 0
    undetected_failures = 0
    for probe in SILENT_FAILURE_SCENARIOS:
        before = base.get(probe.key)
        after = without_verifier.get(probe.key)
        failed = [check.id for check in before.verification if not check.ok] if before else []
        undetected = bool(failed) and after is not None and after.state is RunState.COMPLETED
        if before is None:
            problems.append(f"{probe.key}: the probe did not run")
        elif not before.passed:
            problems.extend(f"{probe.key}: {problem}" for problem in before.problems)
        if not failed:
            problems.append(f"{probe.key}: the Verifier caught no failure")
        if failed and not undetected:
            problems.append(
                f"{probe.key}: the failure was not accepted with the Verifier off"
            )
        caught_failures += 1 if failed else 0
        caught_checks += len(failed)
        undetected_failures += 1 if undetected else 0
        cases.append(
            AblationCase(
                key=probe.key,
                title=probe.title,
                kind="verifier",
                verifier_on_state=before.state if before else None,
                verifier_off_state=after.state if after else None,
                caught_checks=failed,
                undetected=undetected,
            )
        )

    gated_actions = 0
    ungated_actions = 0
    for scenario in SCENARIOS:
        if not scenario.approvals:
            continue
        before = base.get(scenario.key)
        after = without_gates.get(scenario.key)
        gated = [request.tool for request in before.approvals] if before else []
        ungated = [action for action in after.actions if action in set(gated)] if after else []
        if not gated:
            problems.append(f"{scenario.key}: no gated irreversible action in the baseline")
        if after is None:
            problems.append(f"{scenario.key}: the gate ablation did not run")
        elif after.approvals:
            problems.append(f"{scenario.key}: an Approval Request was still created")
        missing = sorted(set(gated) - set(ungated))
        if missing:
            problems.append(
                f"{scenario.key}: gated action(s) {missing} did not execute without a gate"
            )
        gated_actions += len(gated)
        ungated_actions += len(ungated)
        cases.append(
            AblationCase(
                key=scenario.key,
                title=scenario.title,
                kind="approval_gates",
                gated_actions=gated,
                gates_off_state=after.state if after else None,
                ungated_actions=ungated,
            )
        )

    forbidden = without_gates.get("forbidden_action")
    policy_denials = sum(
        1
        for report in gates_off.scenarios
        for escalation in report.escalations
        if "policy denial" in escalation.reason
    )
    if forbidden is None:
        problems.append("forbidden_action: the gate ablation did not run")
    else:
        if forbidden.state is not RunState.NEEDS_HUMAN:
            problems.append("forbidden_action: the policy denial did not hold without gates")
        cases.append(
            AblationCase(
                key=forbidden.key,
                title=forbidden.title,
                kind="approval_gates",
                gates_off_state=forbidden.state,
                gates_off_escalations=len(forbidden.escalations),
            )
        )

    probes = len(SILENT_FAILURE_SCENARIOS)
    return AblationReport(
        probe_failures=probes,
        caught_failures=caught_failures,
        caught_checks=caught_checks,
        detection_rate=caught_failures / probes if probes else 0.0,
        undetected_failures=undetected_failures,
        undetected_rate=undetected_failures / probes if probes else 0.0,
        gated_irreversible_actions=gated_actions,
        ungated_irreversible_actions=ungated_actions,
        policy_denials=policy_denials,
        cases=cases,
        passed=not problems,
        problems=problems,
    )
