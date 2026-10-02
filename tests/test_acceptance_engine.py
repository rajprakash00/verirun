"""Acceptance tests for the Resolve and Plan slice, replayed from recorded LLM fixtures.

Fixtures are recorded with scripts/record_llm_fixtures.py. Replay keeps these
tests deterministic, offline, and free.
"""

from pathlib import Path

import pytest

from company_operator.cli import main
from company_operator.config import Settings
from company_operator.engine.states import RunState
from company_operator.runs.store import RunStore
from tests.support import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "llm"


def replay_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_mode="replay",
        fixture_dir=FIXTURES,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "operator.db",
    )


def test_one_line_invoice_request_cites_the_sop_and_the_spend_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = replay_settings(tmp_path)

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "SOP: invoice-processing" in output
    assert "spend-limits" in output
    assert "over-spend-limit" in output
    assert "Plan" in output
    assert "stopped in state planned" in output


def test_the_planned_run_survives_a_process_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = replay_settings(tmp_path)
    main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
    )
    capsys.readouterr()

    reopened = RunStore(settings.run_db)
    run = reopened.get_run(reopened.list_runs()[0].id)

    assert run.state is RunState.PLANNED
    assert run.work_order.sop == "invoice-processing"
    assert "spend-limits" in run.work_order.policies
    assert run.plan is not None
    step_ids = [step.id for step in run.plan.steps]
    assert len(step_ids) >= 3
    assert len(step_ids) == len(set(step_ids))
    assert all(step.done_criterion for step in run.plan.steps)
    assert all(step.allowed_tools for step in run.plan.steps)


def test_ambiguous_request_asks_instead_of_assuming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = replay_settings(tmp_path)

    code = main(
        ["run", "Handle whatever is going on with the vendor", "--task", "invoice-processing"],
        settings=settings,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "Open questions" in output
    assert "stopped in state needs_human" in output

    stored = RunStore(settings.run_db)
    run = stored.get_run(stored.list_runs()[0].id)
    assert run.state is RunState.NEEDS_HUMAN
    assert run.plan is None
    assert run.work_order.open_questions
    assert any("vendor" in question.lower() for question in run.work_order.open_questions)
    assert any(question in output for question in run.work_order.open_questions)


def test_fixtures_are_committed_and_non_empty() -> None:
    assert FIXTURES.is_dir(), "record LLM fixtures with scripts/record_llm_fixtures.py"
    assert list(FIXTURES.glob("*.json"))
