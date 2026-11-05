"""Acceptance tests for the India GST eval scenarios.

The scenarios run against the in-memory accounting and mail fakes, with the
recorded replay fixtures under ``evals/fixtures/llm``. No Mock Suite database,
Zoho credential, or network is involved.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support import ROOT
from verirun.cli import main
from verirun.config import Settings
from verirun.engine.states import RunState
from verirun.eval import INDIA_CHECKS, INDIA_SCENARIOS, EvalReport, run_eval
from verirun.runs.store import RunStore

INDIA_CHECK_IDS = set(INDIA_CHECKS)


def eval_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_mode="replay",
        fixture_dir=ROOT / "evals" / "fixtures" / "llm",
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
        mail_db=tmp_path / "state" / "maildesk.db",
        erp_db=tmp_path / "state" / "ledgerlite.db",
    )


def test_the_india_eval_runs_all_scenarios_with_the_fake_adapter(
    tmp_path: Path,
) -> None:
    settings = eval_settings(tmp_path)

    report = run_eval(settings, scenarios=INDIA_SCENARIOS, browser=False)

    assert report.passed is True
    assert report.task is None
    assert len(report.scenarios) == len(INDIA_SCENARIOS) == 8
    assert report.totals.completed == 5
    assert report.totals.latency_ms > 0
    assert all(scenario.latency_ms > 0 for scenario in report.scenarios)
    # The fake adapter needs no Mock Suite database.
    assert not settings.mail_db.exists()
    assert not settings.erp_db.exists()

    by_key = {scenario.key: scenario for scenario in report.scenarios}
    completed_keys = (
        "india_happy",
        "india_missing_po",
        "india_over_limit",
        "india_low_confidence",
        "india_transient",
    )
    for key in completed_keys:
        scenario = by_key[key]
        assert scenario.state is RunState.COMPLETED, (key, scenario.problems)
        assert {check.id for check in scenario.verification} == INDIA_CHECK_IDS
        assert all(check.ok for check in scenario.verification), key
        assert scenario.verification_passed is True, key

    happy = by_key["india_happy"]
    assert happy.human_interventions == 0

    for key in ("india_missing_po", "india_over_limit", "india_low_confidence"):
        scenario = by_key[key]
        assert scenario.parked_state is RunState.AWAITING_APPROVAL, key
        assert [request.status for request in scenario.approvals] == ["approved"], key
        assert all(request.executed for request in scenario.approvals), key
        assert scenario.resumed is True, key
        assert scenario.resume_correct is True, key
        assert scenario.human_interventions == 1, key

    assert by_key["india_missing_po"].approvals[0].rule == (
        "missing-purchase-order-needs-approval"
    )
    assert by_key["india_over_limit"].approvals[0].rule == "bill-approval-threshold"
    assert by_key["india_low_confidence"].approvals[0].rule == (
        "low-confidence-extraction-needs-approval"
    )

    duplicate = by_key["india_duplicate"]
    assert duplicate.state is RunState.NEEDS_HUMAN
    assert len(duplicate.escalations) == 1
    assert "duplicate invoice" in duplicate.escalations[0].reason
    assert duplicate.verification == []

    gstin = by_key["india_gstin_mismatch"]
    assert gstin.state is RunState.FAILED
    assert {check.id for check in gstin.verification if not check.ok} == {
        "bill-filed",
        "accounting-invoice-matches",
    }
    assert gstin.parked_state is RunState.AWAITING_APPROVAL
    assert [request.rule for request in gstin.approvals] == [
        "bill-vendor-differs-from-extraction"
    ]
    assert gstin.resumed is True

    tax = by_key["india_tax_mismatch"]
    assert tax.state is RunState.FAILED
    assert {check.id for check in tax.verification if not check.ok} == {"bill-filed"}
    assert tax.human_interventions == 0

    transient = by_key["india_transient"]
    assert transient.state is RunState.COMPLETED
    store = RunStore(settings.run_db)
    attempts = [
        (item.ok, item.error_kind, item.attempt)
        for item in store.list_observations(transient.run_id)
        if item.tool == "erp.file_invoice"
    ]
    assert attempts == [(False, "transient", 1), (True, None, 2)]

    restored = EvalReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_the_india_eval_command_writes_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "india-report.json"

    code = main(
        [
            "eval",
            "--task",
            "invoice-processing-in",
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
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["task"] == "invoice-processing-in"
    assert payload["passed"] is True
    assert len(payload["scenarios"]) == 8
    assert payload["totals"]["latency_ms"] > 0
    assert "8/8 passed" in output
    assert "bill-filed" in output
    assert "ms" in output


def test_an_unexpected_india_outcome_fails_the_eval(tmp_path: Path) -> None:
    settings = eval_settings(tmp_path)
    broken = replace(INDIA_SCENARIOS[0], expected_state=RunState.NEEDS_HUMAN)

    report = run_eval(settings, scenarios=(broken,), browser=False)

    assert report.passed is False
    assert len(report.scenarios) == 1
    scenario = report.scenarios[0]
    assert scenario.state is RunState.COMPLETED
    assert scenario.expected_state is RunState.NEEDS_HUMAN
    assert scenario.problems


def test_an_unknown_task_is_reported_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "eval",
            "--task",
            "no-such-task",
            "--state-dir",
            str(tmp_path / "state"),
            "--shared",
            str(tmp_path / "shared"),
            "--runs",
            str(tmp_path / "runs"),
            "--report",
            str(tmp_path / "report.json"),
            "--no-browser",
        ]
    )

    assert code == 1
    assert "no eval scenarios for Task Pack 'no-such-task'" in capsys.readouterr().err
