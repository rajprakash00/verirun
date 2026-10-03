"""Acceptance tests for Execute and Observe: the ReAct loop, the journal,
checkpoints, per-run limits, and failure classification.

The LLM is scripted, so these tests are deterministic and offline. Tools are
either the real file tools or small doubles that count their invocations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from company_operator.config import ModelPrice
from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.execute import action_key, execute_run
from company_operator.engine.models import Observation, Plan
from company_operator.engine.orchestrator import start_run
from company_operator.engine.states import RunState
from company_operator.llm.client import Usage
from company_operator.runs.store import RunStore
from company_operator.tools import Tool, ToolError, ToolRegistry, build_file_tools
from company_operator.tools.browser import ClickTool, SelectTool, SnapshotTool, TypeTool
from tests.support import ROOT, WORK_ORDER, ScriptedClient, text_turn, tool_turn

NO_ARGUMENTS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

EXECUTE_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Write the invoice summary",
            "allowed_tools": ["files.write"],
            "done_criterion": "the summary file exists",
        },
        {
            "id": "step-2",
            "goal": "Read the summary back",
            "allowed_tools": ["files.read"],
            "done_criterion": "the summary has been read",
        },
    ]
}


class InvoiceTool(Tool):
    """A journaled side-effect tool. Counts every actual execution."""

    name = "erp.file_invoice"
    description = "File an invoice in LedgerLite."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ["invoice_id"],
        "additionalProperties": False,
    }
    side_effect = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls.append(dict(args))
        return Observation(
            ok=True,
            summary=f"Filed {args['invoice_id']}",
            data={"invoice_id": args["invoice_id"]},
        )


class PingTool(Tool):
    name = "test.ping"
    description = "A read-only tool that never finishes a Step."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls += 1
        return Observation(ok=True, summary="pong")


class FlakyTool(Tool):
    name = "test.flaky"
    description = "Fails transiently."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS

    def run(self, args: dict[str, Any]) -> Observation:
        raise ToolError("transient", "the UI blinked")


class AmbiguousTool(Tool):
    name = "test.ambiguous"
    description = "Reports ambiguity."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS

    def run(self, args: dict[str, Any]) -> Observation:
        raise ToolError("ambiguity", "two vendors match")


class QuietTool(Tool):
    name = "test.quiet"
    description = "Never actually called."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS

    def run(self, args: dict[str, Any]) -> Observation:
        raise AssertionError("step-level enforcement should have blocked this call")


class FlakyOnceTool(Tool):
    """Fails transiently on the first call, then succeeds."""

    name = "test.flaky_once"
    description = "Fails transiently once."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS
    side_effect = True

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls += 1
        if self.calls == 1:
            raise ToolError("transient", "the UI blinked")
        return Observation(ok=True, summary="pong", data={"calls": self.calls})


class AlwaysFailsTool(Tool):
    name = "test.always_fails"
    description = "Always fails with a rejected form."
    parameters: ClassVar[dict[str, Any]] = NO_ARGUMENTS

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls += 1
        raise ToolError("invalid", "the form was rejected")


class FailingWriteTool(Tool):
    """A ``files.write`` double that always fails, for replan tests."""

    name = "files.write"
    description = "Always fails."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls += 1
        raise ToolError("invalid", "the form was rejected")


class FailingTwiceWriteTool(Tool):
    """Fails twice, then succeeds; for replan-resume tests."""

    name = "files.write"
    description = "Fails twice, then succeeds."
    side_effect = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls += 1
        if self.calls < 3:
            raise ToolError("invalid", "the form was rejected")
        return Observation(ok=True, summary="wrote the file")


def one_step_plan(tool: str, *, step_id: str = "step-1") -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": step_id,
                "goal": f"Use {tool}",
                "allowed_tools": [tool],
                "done_criterion": "the tool succeeded",
            }
        ]
    }


def seed_planned_run(store: RunStore, plan: dict[str, Any], run_id: str = "RUN-0001") -> None:
    """Persist a planned Run without the LLM, for plans with test-only tools."""
    store.create_run("Process the invoices", "test-task", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.save_plan(run_id, Plan.model_validate(plan))
    store.transition(run_id, RunState.PLANNED)


def start_planned_run(store: RunStore, plan: dict[str, Any], run_id: str = "RUN-0001") -> None:
    task_pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")
    context = load_company_context(ROOT / "company")
    client = ScriptedClient([json.dumps(WORK_ORDER), json.dumps(plan)])
    start_run(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        client,
        store,
        run_id=run_id,
    )


def two_step_invoice_plan() -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": "step-1",
                "goal": "File the first invoice",
                "allowed_tools": ["erp.file_invoice"],
                "done_criterion": "INV-1 is filed",
            },
            {
                "id": "step-2",
                "goal": "File the second invoice",
                "allowed_tools": ["erp.file_invoice"],
                "done_criterion": "INV-2 is filed",
            },
        ]
    }


def test_happy_path_executes_every_step_and_hands_off_to_verify(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    start_planned_run(store, EXECUTE_PLAN)
    root = tmp_path / "shared"
    registry = ToolRegistry(
        build_file_tools(root), allowlist=["files.write", "files.read"]
    )
    client = ScriptedClient(
        [
            tool_turn(
                "files.write",
                {"path": "work/invoice.txt", "content": "NW-2026-001 123.45 USD"},
            ),
            text_turn("Summary written."),
            tool_turn("files.read", {"path": "work/invoice.txt"}),
            text_turn("Summary read back."),
        ]
    )

    run = execute_run("RUN-0001", client, store, registry)

    assert run.state is RunState.VERIFYING
    assert (root / "work" / "invoice.txt").read_text(encoding="utf-8") == (
        "NW-2026-001 123.45 USD"
    )
    observations = store.list_observations("RUN-0001")
    assert [(item.step_position, item.tool, item.ok) for item in observations] == [
        (0, "files.write", True),
        (1, "files.read", True),
    ]
    assert store.get_step_states("RUN-0001") == ["done", "done"]
    assert run.steps_used == 2
    assert run.cost_usd == 0.0
    assert store.get_checkpoint("RUN-0001", "step-1") is not None
    assert store.get_checkpoint("RUN-0001", "step-2") is not None


def test_a_crashed_run_resumes_without_repeating_a_completed_side_effect(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    start_planned_run(store, two_step_invoice_plan())
    tool = InvoiceTool()
    registry = ToolRegistry([tool], allowlist=["erp.file_invoice"])
    crashing = ScriptedClient(
        [
            tool_turn("erp.file_invoice", {"invoice_id": "INV-1"}),
            text_turn("INV-1 filed."),
            tool_turn("erp.file_invoice", {"invoice_id": "INV-2"}),
        ]
    )

    with pytest.raises(AssertionError):
        execute_run("RUN-0001", crashing, store, registry)

    crashed = store.get_run("RUN-0001")
    assert crashed.state is RunState.FAILED
    assert tool.calls == [{"invoice_id": "INV-1"}, {"invoice_id": "INV-2"}]
    assert store.get_step_states("RUN-0001") == ["done", "running"]
    journal = store.list_journal("RUN-0001")
    assert [entry.status for entry in journal] == ["done", "done"]
    assert store.get_checkpoint("RUN-0001", "step-1") is not None
    assert store.get_checkpoint("RUN-0001", "step-2") is None

    restart = ScriptedClient(
        [
            tool_turn("erp.file_invoice", {"invoice_id": "INV-2"}),
            text_turn("INV-2 already filed."),
        ]
    )

    run = execute_run("RUN-0001", restart, store, registry)

    assert run.state is RunState.VERIFYING
    assert tool.calls == [{"invoice_id": "INV-1"}, {"invoice_id": "INV-2"}]
    assert len(store.list_journal("RUN-0001")) == 2
    assert store.get_step_states("RUN-0001") == ["done", "done"]
    assert store.get_checkpoint("RUN-0001", "step-2") is not None


def test_completed_side_effects_are_journaled_with_their_result(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    start_planned_run(store, two_step_invoice_plan())
    tool = InvoiceTool()
    registry = ToolRegistry([tool], allowlist=["erp.file_invoice"])
    client = ScriptedClient(
        [
            tool_turn("erp.file_invoice", {"invoice_id": "INV-1"}),
            text_turn("INV-1 filed."),
            tool_turn("erp.file_invoice", {"invoice_id": "INV-2"}),
            text_turn("INV-2 filed."),
        ]
    )

    execute_run("RUN-0001", client, store, registry)

    side_effects = [
        entry
        for entry in store.list_journal("RUN-0001")
        if entry.action == "erp.file_invoice"
    ]
    assert len(side_effects) == 2
    assert all(entry.result is not None for entry in side_effects)


def test_a_run_over_the_step_limit_stops_with_a_clear_reason(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    plan = {
        "steps": [
            {
                "id": "step-1",
                "goal": "Ping forever",
                "allowed_tools": ["test.ping"],
                "done_criterion": "never met",
            }
        ]
    }
    seed_planned_run(store, plan)
    tool = PingTool()
    registry = ToolRegistry([tool], allowlist=["test.ping"])
    client = ScriptedClient([tool_turn("test.ping") for _ in range(10)])

    run = execute_run("RUN-0001", client, store, registry, max_steps=2)

    assert run.state is RunState.LIMIT_REACHED
    assert run.error is not None
    assert "step limit" in run.error
    assert run.steps_used == 2
    assert tool.calls == 2
    assert len(store.list_observations("RUN-0001")) == 2
    assert store.get_step_states("RUN-0001") == ["running"]


def test_a_run_over_the_cost_limit_stops_before_a_side_effect(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    start_planned_run(store, two_step_invoice_plan())
    tool = InvoiceTool()
    registry = ToolRegistry([tool], allowlist=["erp.file_invoice"])
    prices = {"scripted": ModelPrice(input=1_000_000.0, output=1_000_000.0)}
    client = ScriptedClient(
        [tool_turn("erp.file_invoice", {"invoice_id": "INV-1"}, usage=Usage(prompt_tokens=1))]
    )

    run = execute_run(
        "RUN-0001",
        client,
        store,
        registry,
        max_steps=10,
        max_cost_usd=0.5,
        prices=prices,
    )

    assert run.state is RunState.LIMIT_REACHED
    assert run.error is not None
    assert "cost limit" in run.error
    assert run.cost_usd == pytest.approx(1.0)
    assert tool.calls == []
    assert store.list_journal("RUN-0001") == []


def test_a_closing_turn_over_the_cost_limit_still_stops_the_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    plan = {
        "steps": [
            {
                "id": "step-1",
                "goal": "Finish without tools",
                "allowed_tools": ["test.ping"],
                "done_criterion": "the step is done",
            }
        ]
    }
    seed_planned_run(store, plan)
    registry = ToolRegistry([PingTool()], allowlist=["test.ping"])
    prices = {"scripted": ModelPrice(input=1_000_000.0, output=1_000_000.0)}
    client = ScriptedClient([text_turn("Done.", usage=Usage(prompt_tokens=1))])

    run = execute_run(
        "RUN-0001",
        client,
        store,
        registry,
        max_cost_usd=0.5,
        prices=prices,
    )

    assert run.state is RunState.LIMIT_REACHED
    assert run.error is not None
    assert "cost limit" in run.error
    assert run.cost_usd == pytest.approx(1.0)


def test_resume_skips_steps_recorded_in_the_latest_checkpoint(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_planned_run(store, two_step_invoice_plan())
    store.save_checkpoint(
        "RUN-0001",
        "step-1",
        {"phase": "executing", "step_position": 0, "step_id": "step-1"},
    )
    tool = InvoiceTool()
    registry = ToolRegistry([tool], allowlist=["erp.file_invoice"])
    client = ScriptedClient(
        [
            tool_turn("erp.file_invoice", {"invoice_id": "INV-2"}),
            text_turn("INV-2 filed."),
        ]
    )

    run = execute_run("RUN-0001", client, store, registry)

    assert run.state is RunState.VERIFYING
    assert tool.calls == [{"invoice_id": "INV-2"}]


def test_every_failure_class_is_recorded_on_its_observation(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    plan = {
        "steps": [
            {
                "id": "step-1",
                "goal": "Try the tools",
                "allowed_tools": ["test.flaky", "test.ambiguous"],
                "done_criterion": "the tools have been tried",
            }
        ]
    }
    seed_planned_run(store, plan)
    registry = ToolRegistry(
        [FlakyTool(), AmbiguousTool(), QuietTool()],
        allowlist=["test.flaky", "test.ambiguous", "test.quiet"],
    )
    client = ScriptedClient(
        [
            tool_turn("test.flaky"),
            tool_turn("test.ambiguous"),
            tool_turn("test.quiet"),
            text_turn("Tried everything."),
        ]
    )

    run = execute_run(
        "RUN-0001",
        client,
        store,
        registry,
        max_transient_retries=0,
        max_attempts_per_step=4,
    )

    assert run.state is RunState.VERIFYING
    observations = store.list_observations("RUN-0001")
    assert [item.error_kind for item in observations] == [
        "transient",
        "ambiguity",
        "policy",
    ]
    assert [item.tool for item in observations] == [
        "test.flaky",
        "test.ambiguous",
        "test.quiet",
    ]
    assert "outside step" in observations[2].summary


def test_executing_a_finished_run_is_a_no_op(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    start_planned_run(store, two_step_invoice_plan())
    tool = InvoiceTool()
    registry = ToolRegistry([tool], allowlist=["erp.file_invoice"])
    execute_run(
        "RUN-0001",
        ScriptedClient(
            [
                tool_turn("erp.file_invoice", {"invoice_id": "INV-1"}),
                text_turn("done"),
                tool_turn("erp.file_invoice", {"invoice_id": "INV-2"}),
                text_turn("done"),
            ]
        ),
        store,
        registry,
    )

    run = execute_run(
        "RUN-0001",
        ScriptedClient([]),
        store,
        registry,
    )

    assert run.state is RunState.VERIFYING
    assert tool.calls == [{"invoice_id": "INV-1"}, {"invoice_id": "INV-2"}]


def test_action_keys_are_stable_and_step_sensitive() -> None:
    first = action_key("step-1", "files.write", {"path": "a", "content": "b"})
    reordered = action_key("step-1", "files.write", {"content": "b", "path": "a"})
    other_step = action_key("step-2", "files.write", {"path": "a", "content": "b"})
    other_args = action_key("step-1", "files.write", {"path": "a", "content": "c"})

    assert first == reordered
    assert first != other_step
    assert first != other_args


def test_mutating_tools_are_marked_as_side_effects(tmp_path: Path) -> None:
    file_tools = {tool.name: tool for tool in build_file_tools(tmp_path)}

    assert file_tools["files.write"].side_effect
    assert file_tools["files.move"].side_effect
    assert file_tools["files.archive"].side_effect
    assert not file_tools["files.read"].side_effect
    assert not file_tools["files.list"].side_effect
    assert ClickTool.side_effect
    assert TypeTool.side_effect
    assert SelectTool.side_effect
    assert not SnapshotTool.side_effect


def test_a_transient_failure_is_retried_automatically_then_succeeds(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_planned_run(store, one_step_plan("test.flaky_once"))
    tool = FlakyOnceTool()
    registry = ToolRegistry([tool], allowlist=["test.flaky_once"])
    client = ScriptedClient([tool_turn("test.flaky_once"), text_turn("Recovered.")])

    run = execute_run("RUN-0001", client, store, registry)

    assert run.state is RunState.VERIFYING
    assert tool.calls == 2
    observations = store.list_observations("RUN-0001")
    assert [(item.ok, item.error_kind, item.attempt) for item in observations] == [
        (False, "transient", 1),
        (True, None, 2),
    ]
    assert [entry.status for entry in store.list_journal("RUN-0001")] == ["done"]
    assert len(client.calls) == 2


def test_the_model_can_escalate_with_a_question(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_planned_run(store, one_step_plan("test.ping"))
    registry = ToolRegistry([PingTool()], allowlist=["test.ping"])
    client = ScriptedClient(
        [
            tool_turn(
                "task.escalate",
                {
                    "reason": "No purchase order exists for BP-2026-123",
                    "question": "Which purchase order covers BP-2026-123?",
                },
                call_id="e1",
            )
        ]
    )

    run = execute_run("RUN-0001", client, store, registry)

    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation("RUN-0001")
    assert escalation is not None
    assert escalation.question == "Which purchase order covers BP-2026-123?"
    assert "No purchase order" in escalation.reason
    assert [summary.id for summary in store.list_escalated_runs()] == ["RUN-0001"]
    assert store.list_observations("RUN-0001")[-1].tool == "task.escalate"


def test_repeated_failures_are_bounded_when_replanning_is_unavailable(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_planned_run(store, one_step_plan("test.always_fails"))
    tool = AlwaysFailsTool()
    registry = ToolRegistry([tool], allowlist=["test.always_fails"])
    client = ScriptedClient([tool_turn("test.always_fails") for _ in range(10)])

    run = execute_run("RUN-0001", client, store, registry, max_attempts_per_step=3)

    assert run.state is RunState.NEEDS_HUMAN
    assert tool.calls == 3
    assert len(store.list_observations("RUN-0001")) == 3
    escalation = store.open_escalation("RUN-0001")
    assert escalation is not None
    assert "the form was rejected" in escalation.reason


def test_persistent_failures_replan_once_with_context_then_escalate(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    task_pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")
    context = load_company_context(ROOT / "company")
    replan = one_step_plan("files.write")
    replan["steps"][0]["goal"] = "Try another way"
    client = ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(one_step_plan("files.write")),
            tool_turn("files.write", {"path": "a", "content": "b"}),
            json.dumps(replan),
            tool_turn("files.write", {"path": "a", "content": "b"}),
        ]
    )
    start_run(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        client,
        store,
        run_id="RUN-0001",
    )
    tool = FailingWriteTool()
    registry = ToolRegistry([tool], allowlist=["files.write"])

    run = execute_run(
        "RUN-0001",
        client,
        store,
        registry,
        task_pack=task_pack,
        max_attempts_per_step=1,
        max_replans=1,
    )

    assert run.state is RunState.NEEDS_HUMAN
    assert tool.calls == 2
    assert [step.goal for step in run.plan.steps] == ["Try another way"]
    assert store.get_checkpoint("RUN-0001", "replan-1") is not None
    escalation = store.open_escalation("RUN-0001")
    assert escalation is not None
    assert "the form was rejected" in escalation.reason


def test_resuming_after_a_replan_reexecutes_the_replanned_step(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    task_pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")
    context = load_company_context(ROOT / "company")
    replan = one_step_plan("files.write")
    replan["steps"][0]["goal"] = "Try another way"
    starting_client = ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(one_step_plan("files.write")),
            tool_turn("files.write", {"path": "a", "content": "b"}),
            json.dumps(replan),
            tool_turn("files.write", {"path": "a", "content": "b"}),
        ]
    )
    start_run(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        starting_client,
        store,
        run_id="RUN-0001",
    )
    tool = FailingTwiceWriteTool()
    registry = ToolRegistry([tool], allowlist=["files.write"])
    parked = execute_run(
        "RUN-0001",
        starting_client,
        store,
        registry,
        task_pack=task_pack,
        max_attempts_per_step=1,
        max_replans=1,
    )
    assert parked.state is RunState.NEEDS_HUMAN
    assert tool.calls == 2

    run = execute_run(
        "RUN-0001",
        ScriptedClient(
            [
                tool_turn("files.write", {"path": "a", "content": "b"}),
                text_turn("Wrote it."),
            ]
        ),
        store,
        registry,
        task_pack=task_pack,
        max_attempts_per_step=1,
        max_replans=1,
    )

    assert run.state is RunState.VERIFYING
    assert tool.calls == 3
    assert store.get_step_states("RUN-0001") == ["done"]


def test_replans_are_bounded_across_resumes(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    task_pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")
    context = load_company_context(ROOT / "company")
    replan = one_step_plan("files.write")
    replan["steps"][0]["goal"] = "Try another way"
    client = ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(one_step_plan("files.write")),
            tool_turn("files.write", {"path": "a", "content": "b"}),
            json.dumps(replan),
            tool_turn("files.write", {"path": "a", "content": "b"}),
        ]
    )
    start_run(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        client,
        store,
        run_id="RUN-0001",
    )
    tool = FailingWriteTool()
    registry = ToolRegistry([tool], allowlist=["files.write"])
    execute_run(
        "RUN-0001",
        client,
        store,
        registry,
        task_pack=task_pack,
        max_attempts_per_step=1,
        max_replans=1,
    )

    run = execute_run(
        "RUN-0001",
        ScriptedClient([tool_turn("files.write", {"path": "a", "content": "b"})]),
        store,
        registry,
        task_pack=task_pack,
        max_attempts_per_step=1,
        max_replans=1,
    )

    assert run.state is RunState.NEEDS_HUMAN
    assert tool.calls == 3
    assert len([c for c in store.list_escalations("RUN-0001")]) == 2


def test_an_escalated_run_resumes_after_the_human_answers(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "operator.db")
    seed_planned_run(store, one_step_plan("test.ping"))
    registry = ToolRegistry([PingTool()], allowlist=["test.ping"])
    parked = execute_run(
        "RUN-0001",
        ScriptedClient(
            [
                tool_turn(
                    "task.escalate",
                    {"reason": "unsure", "question": "Which invoice first?"},
                    call_id="e1",
                )
            ]
        ),
        store,
        registry,
    )
    assert parked.state is RunState.NEEDS_HUMAN

    run = execute_run(
        "RUN-0001",
        ScriptedClient([tool_turn("test.ping"), text_turn("Done.")]),
        store,
        registry,
    )

    assert run.state is RunState.VERIFYING
    assert store.open_escalation("RUN-0001") is None
    assert store.list_escalations("RUN-0001")[0].resolved is True
