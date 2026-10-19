"""Acceptance tests for the eval harness.

The harness runs the seeded scenarios in replay mode against the real Mock
Suite and writes a report of per-Run metrics. The fixtures under
``evals/fixtures/llm`` were recorded from the same scripted clients the
scenarios carry, so the whole eval is deterministic and offline.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support import ROOT
from verirun.cli import main
from verirun.config import Settings
from verirun.engine.states import RunState
from verirun.eval import (
    SCENARIOS,
    SILENT_FAILURE_SCENARIOS,
    EvalReport,
    run_eval,
)
from verirun.tools import BrowserSession

EVAL_FIXTURES = ROOT / "evals" / "fixtures" / "llm"

NEEDS_HUMAN_KEYS = (
    "duplicate",
    "amount_mismatch",
    "missing_po",
    "forbidden_action",
    "vendor_duplicate",
    "vendor_missing_tax_form",
)


@pytest.fixture(scope="module")
def chromium(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Skip the browser test when Chromium is not installed."""
    session = BrowserSession(artifact_dir=tmp_path_factory.mktemp("chromium") / "artifacts")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - report missing system dependencies as a skip
        pytest.skip(
            f"Chromium is unavailable ({exc}); "
            "run `uv run playwright install --with-deps chromium`"
        )
    finally:
        session.close()
    yield


def eval_settings(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    *,
    fixture_dir: Path = EVAL_FIXTURES,
) -> Settings:
    return Settings(
        _env_file=None,
        llm_mode="replay",
        fixture_dir=fixture_dir,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=maildesk_state.shared_root,
        mail_db=maildesk_state.db_path,
        erp_db=ledgerlite_db,
    )


def test_the_eval_fixtures_are_committed() -> None:
    fixtures = sorted(EVAL_FIXTURES.glob("*.json"))

    assert fixtures, f"no replay fixtures in {EVAL_FIXTURES}"


def test_eval_report_matches_known_outcomes(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)

    report = run_eval(settings, browser=False)

    assert report.passed is True
    assert report.mode == "replay"
    assert report.browser is False
    assert len(report.scenarios) == len(SCENARIOS) == 12
    by_key = {scenario.key: scenario for scenario in report.scenarios}

    assert by_key["ui_changed"].skipped is True
    assert by_key["ui_changed"].passed is False

    happy = by_key["happy"]
    assert happy.state is RunState.COMPLETED
    assert {check.id for check in happy.verification} == {
        "invoice-filed",
        "payment-scheduled",
        "source-archived",
    }
    assert all(check.ok for check in happy.verification)
    assert happy.verification_passed is True
    assert happy.steps > 0
    assert happy.cost_usd > 0
    assert happy.human_interventions == 0

    transient = by_key["transient"]
    assert transient.passed is True
    assert transient.state is RunState.COMPLETED
    assert transient.problems == []

    over_limit = by_key["over_limit"]
    assert over_limit.state is RunState.COMPLETED
    assert over_limit.parked_state is RunState.AWAITING_APPROVAL
    assert [request.status for request in over_limit.approvals] == ["approved"]
    assert all(request.executed for request in over_limit.approvals)
    assert over_limit.resumed is True
    assert over_limit.resume_correct is True
    assert over_limit.human_interventions == 1

    for key in NEEDS_HUMAN_KEYS:
        scenario = by_key[key]
        assert scenario.state is RunState.NEEDS_HUMAN, key
        assert scenario.passed is True, (key, scenario.problems)
        assert len(scenario.escalations) == 1, key
        assert scenario.human_interventions == 1, key
    assert "policy denial" in by_key["forbidden_action"].escalations[0].reason

    vendor = by_key["vendor_onboarding"]
    assert vendor.state is RunState.COMPLETED
    assert {check.id for check in vendor.verification} == {
        "vendor-created",
        "tax-document-archived",
    }
    assert vendor.resumed is True
    assert vendor.resume_correct is True

    totals = report.totals
    assert totals.scenarios == 12
    assert totals.passed == 11
    assert totals.failed == 0
    assert totals.skipped == 1
    assert totals.completed == 5
    assert totals.steps == sum(scenario.steps for scenario in report.scenarios)
    assert totals.cost_usd > 0

    restored = EvalReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_eval_command_writes_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"

    code = main(
        [
            "eval",
            "--state-dir",
            str(tmp_path / "state"),
            "--shared",
            str(tmp_path / "shared"),
            "--runs",
            str(tmp_path / "runs"),
            "--report",
            str(report_path),
            "--no-browser",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert report_path.is_file()
    payload = report_path.read_text(encoding="utf-8")
    assert '"passed": true' in payload
    assert '"ablations"' in payload
    assert "undetected" in payload
    assert str(report_path) in output
    assert "Ablations:" in output


def test_eval_command_accepts_the_ablation_switches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    verifier_off = tmp_path / "verifier-off.json"
    gates_off = tmp_path / "gates-off.json"

    assert (
        main(
            [
                "eval",
                "--state-dir",
                str(tmp_path / "state"),
                "--shared",
                str(tmp_path / "shared"),
                "--runs",
                str(tmp_path / "runs"),
                "--report",
                str(verifier_off),
                "--no-browser",
                "--no-verifier",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "eval",
                "--state-dir",
                str(tmp_path / "state"),
                "--shared",
                str(tmp_path / "shared"),
                "--runs",
                str(tmp_path / "runs"),
                "--report",
                str(gates_off),
                "--no-browser",
                "--no-approval-gates",
            ]
        )
        == 0
    )

    verifier_payload = verifier_off.read_text(encoding="utf-8")
    gates_payload = gates_off.read_text(encoding="utf-8")
    assert '"verifier": false' in verifier_payload
    assert '"ablations": null' in verifier_payload
    assert '"approval_gates": false' in gates_payload
    assert '"ablations": null' in gates_payload
    capsys.readouterr()


def test_eval_includes_the_regrounding_scenario(
    chromium: None, tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)
    scenario = next(item for item in SCENARIOS if item.key == "ui_changed")

    report = run_eval(settings, scenarios=(scenario,), browser=True)

    (result,) = report.scenarios
    assert result.passed is True, result.problems
    assert result.state is RunState.COMPLETED
    assert result.regrounded_actions >= 1
    assert report.passed is True


def test_eval_reports_failures_instead_of_crashing(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(
        tmp_path, ledgerlite_db, maildesk_state, fixture_dir=tmp_path / "empty"
    )

    report = run_eval(settings, browser=False)

    assert isinstance(report, EvalReport)
    assert report.passed is False
    assert report.scenarios
    runnable = [scenario for scenario in report.scenarios if not scenario.skipped]
    assert all(scenario.problems for scenario in runnable)
    assert any(
        problem.startswith("scenario crashed:")
        for scenario in runnable
        for problem in scenario.problems
    )


def test_a_broken_scenario_does_not_abort_the_eval(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)
    broken = replace(SCENARIOS[0], key="broken", task_id="no-such-task")

    report = run_eval(settings, scenarios=(broken,), browser=False)

    assert report.passed is False
    assert len(report.scenarios) == 1
    assert report.scenarios[0].problems
    assert report.scenarios[0].passed is False


def test_the_report_contrasts_caught_and_undetected_failures(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)

    report = run_eval(settings, browser=False)

    assert report.verifier is True
    assert report.approval_gates is True
    ablation = report.ablations
    assert ablation is not None
    assert ablation.passed is True
    assert ablation.problems == []
    assert ablation.probe_failures == 2
    assert ablation.caught_failures == 2
    assert ablation.caught_checks == 4
    assert ablation.detection_rate == 1.0
    assert ablation.undetected_failures == 2
    assert ablation.undetected_rate == 1.0
    assert ablation.gated_irreversible_actions == 2
    assert ablation.ungated_irreversible_actions == 2
    assert ablation.policy_denials == 1

    cases = {case.key: case for case in ablation.cases}

    phantom = cases["phantom_completion"]
    assert phantom.verifier_on_state is RunState.FAILED
    assert phantom.verifier_off_state is RunState.COMPLETED
    assert phantom.caught_checks == [
        "invoice-filed",
        "payment-scheduled",
        "source-archived",
    ]
    assert phantom.undetected is True

    wrong_vendor = cases["wrong_vendor"]
    assert wrong_vendor.verifier_on_state is RunState.FAILED
    assert wrong_vendor.verifier_off_state is RunState.COMPLETED
    assert wrong_vendor.caught_checks == ["invoice-filed"]
    assert wrong_vendor.undetected is True

    over_limit = cases["over_limit"]
    assert over_limit.gated_actions == ["erp.schedule_payment"]
    assert over_limit.gates_off_state is RunState.COMPLETED
    assert over_limit.ungated_actions == ["erp.schedule_payment"]

    vendor = cases["vendor_onboarding"]
    assert vendor.gated_actions == ["erp.create_vendor"]
    assert vendor.gates_off_state is RunState.COMPLETED
    assert vendor.ungated_actions == ["erp.create_vendor"]

    forbidden = cases["forbidden_action"]
    assert forbidden.gates_off_state is RunState.NEEDS_HUMAN
    assert forbidden.gates_off_escalations == 1
    assert forbidden.ungated_actions == []

    restored = EvalReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_the_verifier_switch_leaves_probe_failures_undetected(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)

    report = run_eval(settings, browser=False, verifier=False)

    assert report.verifier is False
    assert report.approval_gates is True
    assert report.ablations is None
    by_key = {scenario.key: scenario for scenario in report.scenarios}
    assert set(by_key) >= {scenario.key for scenario in SILENT_FAILURE_SCENARIOS}

    phantom = by_key["phantom_completion"]
    assert phantom.state is RunState.COMPLETED
    assert phantom.verification == []

    wrong_vendor = by_key["wrong_vendor"]
    assert wrong_vendor.state is RunState.COMPLETED
    assert wrong_vendor.verification == []


def test_the_approval_gate_switch_submits_irreversible_actions(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)

    report = run_eval(settings, browser=False, approval_gates=False)

    assert report.verifier is True
    assert report.approval_gates is False
    assert report.ablations is None
    by_key = {scenario.key: scenario for scenario in report.scenarios}

    over_limit = by_key["over_limit"]
    assert over_limit.state is RunState.COMPLETED
    assert over_limit.parked_state is None
    assert over_limit.approvals == []
    assert "erp.schedule_payment" in over_limit.actions
    assert over_limit.verification_passed is True

    vendor = by_key["vendor_onboarding"]
    assert vendor.state is RunState.COMPLETED
    assert vendor.parked_state is None
    assert vendor.approvals == []
    assert "erp.create_vendor" in vendor.actions

    forbidden = by_key["forbidden_action"]
    assert forbidden.state is RunState.NEEDS_HUMAN
    assert "policy denial" in forbidden.escalations[0].reason


def test_the_eval_switches_are_off_by_default_in_the_product(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state
) -> None:
    settings = eval_settings(tmp_path, ledgerlite_db, maildesk_state)

    report = run_eval(settings, browser=False)

    assert report.verifier is True
    assert report.approval_gates is True
    by_key = {scenario.key: scenario for scenario in report.scenarios}
    assert by_key["over_limit"].parked_state is RunState.AWAITING_APPROVAL
    assert by_key["over_limit"].approvals[0].status == "approved"
