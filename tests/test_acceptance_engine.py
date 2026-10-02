"""Acceptance tests for the Resolve and Plan slice, replayed from recorded LLM fixtures.

Fixtures are recorded with scripts/record_llm_fixtures.py. Replay keeps these
tests deterministic, offline, and free. The CLI now runs the whole pipeline, so
these slice tests drive the engine's Resolve and Plan phases directly.
"""

from pathlib import Path

import pytest

from company_operator.cli import format_run, main
from company_operator.config import Settings
from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.orchestrator import start_run
from company_operator.engine.states import RunState
from company_operator.llm.client import build_client
from company_operator.runs.store import RunStore, generate_run_id
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


def start_replayed_run(settings: Settings, request: str) -> tuple[RunStore, str]:
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    run_id = generate_run_id()
    store = RunStore(settings.run_db)
    start_run(
        request,
        task_pack,
        context,
        build_client(settings, session_id=run_id),
        store,
        run_id=run_id,
    )
    return store, run_id


def test_one_line_invoice_request_cites_the_sop_and_the_spend_limit(tmp_path: Path) -> None:
    settings = replay_settings(tmp_path)

    store, run_id = start_replayed_run(settings, "Process the invoices in the AP mailbox")
    run = store.get_run(run_id)
    output = format_run(run)

    assert run.state is RunState.PLANNED
    assert "SOP: invoice-processing" in output
    assert "spend-limits" in output
    assert "over-spend-limit" in output
    assert "Plan" in output
    assert "stopped in state planned" in output


def test_the_planned_run_survives_a_process_restart(tmp_path: Path) -> None:
    settings = replay_settings(tmp_path)
    _, run_id = start_replayed_run(settings, "Process the invoices in the AP mailbox")

    reopened = RunStore(settings.run_db)
    run = reopened.get_run(run_id)

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
