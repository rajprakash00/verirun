import json
import shutil
from pathlib import Path

import pytest

from company_operator.cli import main
from company_operator.config import Settings
from company_operator.engine.states import RunState
from company_operator.runs.store import RunStore
from tests.support import PLAN, ROOT, WORK_ORDER, ScriptedClient


def make_settings(tmp_path: Path) -> Settings:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shutil.copy(ROOT / "tasks" / "invoice-processing.yaml", tasks_dir)
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=tasks_dir,
        run_db=tmp_path / "runs" / "operator.db",
    )


def test_run_resolves_plans_and_persists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    client = ScriptedClient([json.dumps(WORK_ORDER), json.dumps(PLAN)])

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=client,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Work Order" in output
    assert "SOP: invoice-processing" in output
    assert "Policies: spend-limits, action-rules" in output
    assert "Plan" in output
    assert "step-1" in output
    assert "stopped in state planned" in output

    runs = RunStore(settings.run_db).list_runs()
    assert len(runs) == 1
    assert runs[0].state is RunState.PLANNED


def test_run_and_plan_survive_a_process_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=ScriptedClient([json.dumps(WORK_ORDER), json.dumps(PLAN)]),
    )
    capsys.readouterr()

    reopened = RunStore(settings.run_db)
    run = reopened.list_runs()[0]
    stored = reopened.get_run(run.id)

    assert stored.state is RunState.PLANNED
    assert stored.work_order.policies == ["spend-limits", "action-rules"]
    assert [step.id for step in stored.plan.steps] == ["step-1", "step-2", "step-3"]


def test_ambiguous_request_stops_with_an_open_question(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    ambiguous = dict(WORK_ORDER, open_questions=["Which invoice should be processed?"])

    code = main(
        ["run", "Handle the invoice situation", "--task", "invoice-processing"],
        settings=settings,
        client=ScriptedClient([json.dumps(ambiguous)]),
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "Open questions" in output
    assert "Which invoice should be processed?" in output
    assert "needs_human" in output

    run = RunStore(settings.run_db).list_runs()[0]
    assert run.state is RunState.NEEDS_HUMAN


def test_unknown_task_pack_is_rejected_with_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)

    code = main(
        ["run", "Process the invoices", "--task", "no-such-task"],
        settings=settings,
        client=ScriptedClient([]),
    )

    error = capsys.readouterr().err
    assert code == 1
    assert "no-such-task.yaml" in error
    assert "not found" in error


def test_invalid_task_pack_is_rejected_with_a_precise_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    (settings.tasks_dir / "broken.yaml").write_text("id: broken\n", encoding="utf-8")

    code = main(
        ["run", "Process the invoices", "--task", "broken"],
        settings=settings,
        client=ScriptedClient([]),
    )

    error = capsys.readouterr().err
    assert code == 1
    assert "invalid Task Pack" in error
    assert "goal_template: Field required" in error


def test_missing_task_flag_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)

    code = main(["run", "Process the invoices"], settings=settings, client=ScriptedClient([]))

    error = capsys.readouterr().err
    assert code == 2
    assert "--task is required" in error
