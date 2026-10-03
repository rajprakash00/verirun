import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from company_operator.context.models import ApprovalGate, WorkOrder
from company_operator.engine.models import CheckResult, Observation, Plan, Step
from company_operator.engine.states import RunState, StepState
from company_operator.runs.models import ApprovalRequest
from company_operator.runs.store import RunNotFoundError, RunStore


def work_order() -> WorkOrder:
    return WorkOrder(
        sop="invoice-processing",
        goal="File the September invoices",
        assumptions=["Only the September batch is in scope"],
        systems=["maildesk", "ledgerlite"],
        policies=["spend-limits"],
        approval_gates=[
            ApprovalGate(
                id="over-spend-limit",
                description="Payments above the limit wait for a human",
                policy="spend-limits",
            )
        ],
        success_criteria=["Invoice NW-2026-001 exists in LedgerLite"],
        open_questions=[],
    )


def plan() -> Plan:
    return Plan(
        steps=[
            Step(
                id="step-1",
                goal="Read the invoice email",
                allowed_tools=["mail.read"],
                done_criterion="The invoice fields are extracted",
            ),
            Step(
                id="step-2",
                goal="File the invoice",
                allowed_tools=["erp.file_invoice"],
                done_criterion="The invoice exists in LedgerLite",
            ),
        ]
    )


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs.db")


def test_create_run_starts_in_created(store: RunStore) -> None:
    run = store.create_run("Process the invoices", "invoice-processing", run_id="RUN-0001")

    assert run.id == "RUN-0001"
    assert run.state is RunState.CREATED
    assert run.work_order is None
    assert run.plan is None
    assert run.created_at.tzinfo is not None


def test_generated_run_ids_are_unique(store: RunStore) -> None:
    first = store.create_run("one", "invoice-processing")
    second = store.create_run("two", "invoice-processing")

    assert first.id != second.id
    assert first.id.startswith("RUN-")


def test_work_order_and_plan_survive_a_new_store_instance(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    store = RunStore(db_path)
    store.create_run("Process the invoices", "invoice-processing", run_id="RUN-0001")
    store.transition("RUN-0001", RunState.RESOLVING)
    store.save_work_order("RUN-0001", work_order())
    store.save_plan("RUN-0001", plan())
    store.transition("RUN-0001", RunState.PLANNED)

    reopened = RunStore(db_path)
    run = reopened.get_run("RUN-0001")

    assert run.state is RunState.PLANNED
    assert run.work_order == work_order()
    assert run.plan == plan()
    assert [step.id for step in run.plan.steps] == ["step-1", "step-2"]
    assert reopened.get_steps("RUN-0001") == plan().steps


def test_transition_validates_and_updates(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    run = store.transition("RUN-0001", RunState.RESOLVING)

    assert run.state is RunState.RESOLVING
    assert store.get_run("RUN-0001").state is RunState.RESOLVING

    with pytest.raises(ValueError):
        store.transition("RUN-0001", RunState.EXECUTING)


def test_failure_records_the_reason(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.transition("RUN-0001", RunState.RESOLVING)
    store.set_error("RUN-0001", "the model returned invalid JSON")
    run = store.transition("RUN-0001", RunState.FAILED)

    assert run.state is RunState.FAILED
    assert run.error == "the model returned invalid JSON"


def test_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.get_run("RUN-NOPE")


def test_observations_round_trip_in_order(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    first = Observation(ok=False, summary="mailbox busy", error_kind="transient")
    second = Observation(ok=True, summary="listed 3 messages", data={"count": 3})

    first_id = store.add_observation("RUN-0001", first, step_position=0)
    second_id = store.add_observation("RUN-0001", second, step_position=0)
    stored = store.list_observations("RUN-0001")

    assert first_id < second_id
    assert [observation.summary for observation in stored] == [
        "mailbox busy",
        "listed 3 messages",
    ]
    assert stored[0].error_kind == "transient"
    assert stored[1].data == {"count": 3}
    assert stored[1].step_position == 0
    assert stored[1].created_at.tzinfo is not None


def test_observations_carry_their_attempt_number(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    store.add_observation(
        "RUN-0001",
        Observation(ok=False, summary="the UI blinked", error_kind="transient"),
        step_position=0,
        tool="erp.file_invoice",
        attempt=1,
    )
    store.add_observation(
        "RUN-0001",
        Observation(ok=True, summary="filed the invoice"),
        step_position=0,
        tool="erp.file_invoice",
        attempt=2,
    )

    stored = store.list_observations("RUN-0001")
    assert [observation.attempt for observation in stored] == [1, 2]
    assert [observation.tool for observation in stored] == [
        "erp.file_invoice",
        "erp.file_invoice",
    ]


def test_escalation_records_a_question_and_parks_the_run(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.transition("RUN-0001", RunState.RESOLVING)
    store.save_plan("RUN-0001", plan())
    store.transition("RUN-0001", RunState.PLANNED)

    escalation = store.escalate(
        "RUN-0001",
        reason="duplicate invoice: AQ-2026-014 already exists as INV-3002 (paid)",
        question="Invoice AQ-2026-014 already exists as INV-3002. Should it be ignored?",
        context={"existing_invoice_id": "INV-3002"},
    )

    assert escalation.run_id == "RUN-0001"
    assert escalation.resolved is False
    assert escalation.context == {"existing_invoice_id": "INV-3002"}
    assert escalation.created_at.tzinfo is not None
    assert store.get_run("RUN-0001").state is RunState.NEEDS_HUMAN
    assert store.open_escalation("RUN-0001") == escalation


def test_escalated_runs_are_listed_with_their_open_question(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_run("duplicate one", "invoice-processing", run_id="RUN-0001")
    store.transition("RUN-0001", RunState.RESOLVING)
    store.create_run("a quiet one", "invoice-processing", run_id="RUN-0002")
    question = "Invoice AQ-2026-014 already exists as INV-3002. Should it be ignored?"
    store.escalate(
        "RUN-0001",
        reason="duplicate invoice: AQ-2026-014 already exists as INV-3002 (paid)",
        question=question,
    )

    escalated = store.list_escalated_runs()

    assert [(row.id, row.question) for row in escalated] == [("RUN-0001", question)]
    assert escalated[0].state is RunState.NEEDS_HUMAN
    summaries = {row.id: row for row in store.list_runs()}
    assert summaries["RUN-0001"].question == question
    assert summaries["RUN-0002"].question is None


def test_resolving_an_escalation_clears_the_open_question(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.transition("RUN-0001", RunState.RESOLVING)
    store.escalate("RUN-0001", reason="missing PO", question="Which PO covers BP-2026-123?")

    store.resolve_escalations("RUN-0001")

    assert store.open_escalation("RUN-0001") is None
    assert store.list_escalated_runs() == []
    history = store.list_escalations("RUN-0001")
    assert len(history) == 1
    assert history[0].resolved is True
    assert history[0].question == "Which PO covers BP-2026-123?"


def test_journal_is_idempotent_by_key(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    assert store.record_action("RUN-0001", "file-invoice-NW-2026-001", "erp.file_invoice", {"id": "x"})
    assert not store.completed_action("RUN-0001", "file-invoice-NW-2026-001")
    assert store.record_action("RUN-0001", "file-invoice-NW-2026-001", "erp.file_invoice", {})

    store.complete_action("RUN-0001", "file-invoice-NW-2026-001", {"invoice_id": "INV-3001"})

    assert store.completed_action("RUN-0001", "file-invoice-NW-2026-001")
    assert not store.record_action("RUN-0001", "file-invoice-NW-2026-001", "erp.file_invoice", {})
    entries = store.list_journal("RUN-0001")
    assert entries[-1].status == "done"
    assert entries[-1].result == {"invoice_id": "INV-3001"}


def test_step_states_round_trip_in_position_order(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.save_plan("RUN-0001", plan())

    assert store.get_step_states("RUN-0001") == ["pending", "pending"]

    store.set_step_state("RUN-0001", 0, StepState.RUNNING)
    store.set_step_state("RUN-0001", 0, StepState.DONE)

    assert store.get_step_states("RUN-0001") == [StepState.DONE, StepState.PENDING]
    with pytest.raises(KeyError):
        store.set_step_state("RUN-0001", 5, StepState.DONE)


def test_usage_counters_accumulate(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.create_run("request", "invoice-processing", run_id="RUN-0001")

    store.increment_usage("RUN-0001", steps=2, cost_usd=0.25)
    store.increment_usage("RUN-0001", steps=1, cost_usd=0.5)

    reopened = RunStore(tmp_path / "runs.db")
    run = reopened.get_run("RUN-0001")
    assert run.steps_used == 3
    assert run.cost_usd == pytest.approx(0.75)


def test_journal_entry_can_be_fetched_by_key(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.record_action("RUN-0001", "key", "erp.file_invoice", {"id": "x"})
    store.complete_action("RUN-0001", "key", {"ok": True})

    entry = store.get_journal_entry("RUN-0001", "key")

    assert entry is not None
    assert entry.action == "erp.file_invoice"
    assert entry.status == "done"
    assert entry.result == {"ok": True}
    assert store.get_journal_entry("RUN-0001", "missing") is None


OLD_SCHEMA = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    work_order_json TEXT,
    plan_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_position INTEGER,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    error_kind TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


def test_store_migrates_a_database_from_the_previous_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            """
            INSERT INTO runs (id, request, task_id, state, created_at, updated_at)
            VALUES ('RUN-0001', 'request', 'invoice-processing', 'planned', ?, ?)
            """,
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

    store = RunStore(db_path)
    run = store.get_run("RUN-0001")

    assert run.steps_used == 0
    assert run.cost_usd == 0.0
    store.increment_usage("RUN-0001", steps=1, cost_usd=0.5)
    store.add_observation(
        "RUN-0001",
        Observation(ok=True, summary="listed"),
        tool="files.list",
    )
    assert store.get_run("RUN-0001").steps_used == 1
    observations = store.list_observations("RUN-0001")
    assert observations[0].tool == "files.list"
    assert observations[0].attempt == 1
    store.escalate("RUN-0001", reason="old database", question="Does migration hold?")
    assert store.open_escalation("RUN-0001").question == "Does migration hold?"


def test_verification_results_round_trip_in_order(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    results = [
        CheckResult(
            id="invoice-filed",
            description="The invoice exists in LedgerLite.",
            ok=True,
            detail="INV-3003 matches the source document",
            evidence=["erp:INV-3003", "source:documents/invoices/NW-2026-001.pdf"],
        ),
        CheckResult(
            id="source-archived",
            description="The source is archived.",
            ok=False,
            detail="no archive action was recorded",
        ),
    ]

    store.save_verification("RUN-0001", results)

    assert store.get_verification("RUN-0001") == results
    assert store.get_verification("RUN-NOPE") == []


def test_saving_verification_replaces_the_previous_results(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.save_verification(
        "RUN-0001",
        [CheckResult(id="a", description="first", ok=False, detail="old")],
    )

    store.save_verification(
        "RUN-0001",
        [CheckResult(id="b", description="second", ok=True, detail="new")],
    )

    stored = store.get_verification("RUN-0001")
    assert [result.id for result in stored] == ["b"]
    assert stored[0].detail == "new"


def test_failed_action_can_be_retried(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.record_action("RUN-0001", "key", "erp.file_invoice", {})
    store.fail_action("RUN-0001", "key", "timeout")

    assert not store.completed_action("RUN-0001", "key")
    assert store.record_action("RUN-0001", "key", "erp.file_invoice", {})
    assert store.completed_action("RUN-0001", "key") is False


def seed_executing_run(store: RunStore, run_id: str = "RUN-0001") -> None:
    store.create_run("Process the invoices", "invoice-processing", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.save_plan(run_id, plan())
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)


def approval(store: RunStore, **overrides) -> ApprovalRequest:
    fields = {
        "key": "step-2:erp.schedule_payment:abcd1234",
        "step_position": 1,
        "step_id": "step-2",
        "tool": "erp.schedule_payment",
        "action": "payment.schedule",
        "arguments": {"invoice_id": "INV-3005"},
        "policy": "spend-limits",
        "rule": "approval-threshold",
        "reason": "Any payment above 10,000.00 USD must be held at an approval gate.",
    }
    fields.update(overrides)
    return store.create_approval("RUN-0001", **fields)


def test_an_approval_request_parks_the_run_and_round_trips(store: RunStore) -> None:
    seed_executing_run(store)

    request = approval(store)
    store.transition("RUN-0001", RunState.AWAITING_APPROVAL)

    assert request.id > 0
    assert request.run_id == "RUN-0001"
    assert request.tool == "erp.schedule_payment"
    assert request.arguments == {"invoice_id": "INV-3005"}
    assert request.status == "pending"
    assert request.decided_at is None
    assert request.executed_at is None
    assert request.created_at.tzinfo is not None
    assert store.get_run("RUN-0001").state is RunState.AWAITING_APPROVAL
    assert store.get_approval(request.id) == request
    assert store.open_approval("RUN-0001") == request
    assert store.list_pending_approvals() == [request]


def test_creating_the_same_prepared_action_twice_returns_one_request(store: RunStore) -> None:
    seed_executing_run(store)

    first = approval(store)
    second = approval(store)

    assert first.id == second.id
    assert len(store.list_approvals("RUN-0001")) == 1


def test_approving_records_the_decision_time_and_releases_the_queue(store: RunStore) -> None:
    seed_executing_run(store)
    request = approval(store)
    store.transition("RUN-0001", RunState.AWAITING_APPROVAL)

    decided = store.decide_approval(request.id, approved=True)

    assert decided.status == "approved"
    assert decided.decided_at is not None
    assert decided.decision_reason is None
    assert store.open_approval("RUN-0001") is None
    assert store.list_pending_approvals() == []
    assert store.unsubmitted_approvals("RUN-0001", 1) == [decided]
    assert store.approved_approval_for("RUN-0001", request.key) == decided
    with pytest.raises(ValueError):
        store.decide_approval(request.id, approved=False, reason="too late")


def test_rejecting_records_the_reason(store: RunStore) -> None:
    seed_executing_run(store)
    request = approval(store)
    store.transition("RUN-0001", RunState.AWAITING_APPROVAL)

    decided = store.decide_approval(request.id, approved=False, reason="Budget is frozen")

    assert decided.status == "rejected"
    assert decided.decision_reason == "Budget is frozen"
    assert store.open_approval("RUN-0001") is None
    assert store.unsubmitted_approvals("RUN-0001", 1) == []
    assert store.approved_approval_for("RUN-0001", request.key) is None


def test_an_approved_action_can_be_marked_submitted_once(store: RunStore) -> None:
    seed_executing_run(store)
    request = approval(store)
    store.transition("RUN-0001", RunState.AWAITING_APPROVAL)
    store.decide_approval(request.id, approved=True)

    store.mark_approval_executed(request.id)

    submitted = store.get_approval(request.id)
    assert submitted.executed_at is not None
    with pytest.raises(ValueError):
        store.mark_approval_executed(request.id)


def test_unsubmitted_approved_actions_are_listed_for_their_step(store: RunStore) -> None:
    seed_executing_run(store)
    first = approval(store)
    approval(
        store,
        key="step-2:erp.file_invoice:9999",
        tool="erp.file_invoice",
        action="invoice.file",
        arguments={"number": "NW-2026-001"},
    )
    store.transition("RUN-0001", RunState.AWAITING_APPROVAL)
    decided = store.decide_approval(first.id, approved=True)

    assert store.unsubmitted_approvals("RUN-0001", 1) == [decided]

    store.mark_approval_executed(first.id)

    assert store.unsubmitted_approvals("RUN-0001", 1) == []
    assert store.unsubmitted_approvals("RUN-0001", 0) == []


def test_unknown_approvals_raise(store: RunStore) -> None:
    with pytest.raises(KeyError):
        store.get_approval(999)
    with pytest.raises(KeyError):
        store.decide_approval(999, approved=True)


def test_checkpoints_round_trip_and_latest_wins(store: RunStore) -> None:
    store.create_run("request", "invoice-processing", run_id="RUN-0001")
    store.save_checkpoint("RUN-0001", "after-resolve", {"phase": "resolving", "step": 0})
    store.save_checkpoint("RUN-0001", "after-step-1", {"phase": "executing", "step": 1})

    assert store.get_checkpoint("RUN-0001", "after-resolve") == {
        "phase": "resolving",
        "step": 0,
    }
    latest = store.latest_checkpoint("RUN-0001")
    assert latest is not None
    assert latest.label == "after-step-1"
    assert latest.state == {"phase": "executing", "step": 1}
    assert latest.created_at.tzinfo is not None
    assert store.get_checkpoint("RUN-0001", "missing") is None
    assert store.latest_checkpoint("RUN-0001").created_at >= datetime(2020, 1, 1, tzinfo=UTC)
    assert store.count_checkpoints("RUN-0001") == 2
    store.save_checkpoint("RUN-0001", "replan-1", {"phase": "replanning"})
    assert store.count_checkpoints("RUN-0001", prefix="replan-") == 1
