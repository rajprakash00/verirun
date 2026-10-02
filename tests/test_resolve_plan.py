import json

import pytest

from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.orchestrator import start_run
from company_operator.engine.plan import plan_run
from company_operator.engine.resolve import resolve
from company_operator.engine.states import RunState
from company_operator.engine.structured import StructuredOutputError
from company_operator.runs.store import RunStore
from tests.support import PLAN, ROOT, WORK_ORDER, ScriptedClient


@pytest.fixture
def context():
    return load_company_context(ROOT / "company")


@pytest.fixture
def task_pack():
    return load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")


def test_resolve_cites_the_context_policies_and_systems(context, task_pack) -> None:
    client = ScriptedClient([json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices in the AP mailbox", task_pack, context, client)

    assert work_order.sop == "invoice-processing"
    assert work_order.policies == ["spend-limits", "action-rules"]
    assert work_order.systems == ["maildesk", "ledgerlite"]
    assert work_order.approval_gates[0].policy == "spend-limits"
    call = client.calls[0]
    assert call["model_role"] == "reason"
    assert call["response_format"] == {"type": "json_object"}


def test_resolve_repairs_a_citation_outside_the_task_pack(context, task_pack) -> None:
    bad = dict(WORK_ORDER, policies=["made-up-policy"])
    client = ScriptedClient([json.dumps(bad), json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices in the AP mailbox", task_pack, context, client)

    assert work_order.policies == ["spend-limits", "action-rules"]
    assert len(client.calls) == 2


def test_resolve_requires_at_least_one_policy_when_the_pack_has_them(context, task_pack) -> None:
    bad = dict(WORK_ORDER, policies=[], approval_gates=[])
    client = ScriptedClient([json.dumps(bad), json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices", task_pack, context, client)

    assert work_order.policies == ["spend-limits", "action-rules"]
    assert len(client.calls) == 2


def test_resolve_requires_approval_gates_to_match_the_task_pack(context, task_pack) -> None:
    bad = dict(
        WORK_ORDER,
        approval_gates=[
            {"id": "made-up-gate", "description": "Nope", "policy": "spend-limits"}
        ],
    )
    client = ScriptedClient([json.dumps(bad), json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices", task_pack, context, client)

    assert [gate.id for gate in work_order.approval_gates] == ["over-spend-limit"]
    assert len(client.calls) == 2


def test_resolve_requires_gate_policies_to_be_cited(context, task_pack) -> None:
    bad = dict(WORK_ORDER, policies=["action-rules"])
    client = ScriptedClient([json.dumps(bad), json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices", task_pack, context, client)

    assert "spend-limits" in work_order.policies
    assert len(client.calls) == 2


def test_resolve_rejects_an_unknown_system(context, task_pack) -> None:
    bad = dict(WORK_ORDER, systems=["maildesk", "mainframe"])
    client = ScriptedClient([json.dumps(bad), json.dumps(WORK_ORDER)])

    work_order = resolve("Process the invoices", task_pack, context, client)

    assert work_order.systems == ["maildesk", "ledgerlite"]
    assert len(client.calls) == 2


def test_resolve_fails_clearly_after_the_repair_budget(context, task_pack) -> None:
    client = ScriptedClient(["not json", "still not json"])

    with pytest.raises(StructuredOutputError, match="did not produce valid structured output"):
        resolve("Process the invoices", task_pack, context, client)


def test_plan_orders_steps_and_enforces_the_tool_allowlist(context, task_pack) -> None:
    work_order = resolve("Process the invoices", task_pack, context, ScriptedClient([json.dumps(WORK_ORDER)]))
    bad = json.loads(json.dumps(PLAN))
    bad["steps"][0]["allowed_tools"] = ["mainframe.launch"]
    client = ScriptedClient([json.dumps(bad), json.dumps(PLAN)])

    plan = plan_run(work_order, task_pack, client)

    assert [step.id for step in plan.steps] == ["step-1", "step-2", "step-3"]
    assert plan.steps[2].allowed_tools == ["erp.file_invoice", "erp.schedule_payment"]
    assert len(client.calls) == 2


def test_plan_rejects_duplicate_step_ids(context, task_pack) -> None:
    work_order = resolve("Process the invoices", task_pack, context, ScriptedClient([json.dumps(WORK_ORDER)]))
    bad = json.loads(json.dumps(PLAN))
    bad["steps"][1]["id"] = "step-1"
    client = ScriptedClient([json.dumps(bad), json.dumps(PLAN)])

    plan = plan_run(work_order, task_pack, client)

    assert [step.id for step in plan.steps] == ["step-1", "step-2", "step-3"]


def test_start_run_resolves_plans_and_persists(tmp_path, context, task_pack) -> None:
    store = RunStore(tmp_path / "runs.db")
    client = ScriptedClient([json.dumps(WORK_ORDER), json.dumps(PLAN)])

    run = start_run(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        client,
        store,
        run_id="RUN-0001",
    )

    assert run.state is RunState.PLANNED
    assert run.work_order is not None
    assert run.work_order.policies == ["spend-limits", "action-rules"]
    assert run.plan == plan_run(
        run.work_order, task_pack, ScriptedClient([json.dumps(PLAN)])
    )
    reopened = RunStore(tmp_path / "runs.db")
    assert reopened.get_run("RUN-0001").state is RunState.PLANNED
    assert reopened.get_run("RUN-0001").plan.steps[0].id == "step-1"


def test_start_run_escalates_an_ambiguous_request_without_planning(tmp_path, context, task_pack) -> None:
    ambiguous = dict(WORK_ORDER, open_questions=["Which invoice should be processed?"])
    store = RunStore(tmp_path / "runs.db")
    client = ScriptedClient([json.dumps(ambiguous)])

    run = start_run("Handle the invoice situation", task_pack, context, client, store, run_id="RUN-0001")

    assert run.state is RunState.NEEDS_HUMAN
    assert run.work_order.open_questions == ["Which invoice should be processed?"]
    assert run.plan is None
    assert len(client.calls) == 1


def test_start_run_marks_the_run_failed_on_invalid_output(tmp_path, context, task_pack) -> None:
    store = RunStore(tmp_path / "runs.db")
    client = ScriptedClient(["nope", "nope"])

    with pytest.raises(StructuredOutputError):
        start_run("Process the invoices", task_pack, context, client, store, run_id="RUN-0001")

    run = store.get_run("RUN-0001")
    assert run.state is RunState.FAILED
    assert "valid structured output" in run.error


def test_start_run_rejects_a_task_pack_with_missing_context(tmp_path, context, task_pack) -> None:
    store = RunStore(tmp_path / "runs.db")
    broken = task_pack.model_copy(update={"sop": "no-such-sop"})

    with pytest.raises(Exception, match="no-such-sop"):
        start_run("Process the invoices", broken, context, ScriptedClient([]), store, run_id="RUN-0001")

    assert store.list_runs() == []
