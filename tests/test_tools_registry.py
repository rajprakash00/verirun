"""Unit tests for the tool protocol, the allowlist registry, and the policy gate."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any, ClassVar

import pytest

from mocks.ledgerlite import db
from tests.support import ROOT
from verirun.context.company import load_company_context
from verirun.engine.models import Observation
from verirun.tools import PolicyGate, Tool, ToolError, ToolRegistry


class RecordingTool(Tool):
    """A side-effecting tool double. It records every invocation."""

    name = "erp.schedule_payment"
    description = "Schedule a payment in LedgerLite."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ["invoice_id"],
        "additionalProperties": False,
    }
    action = "payment.schedule"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "vendor_status": args.get("vendor_status", "active"),
            "amount_usd": args.get("amount_usd", 0),
            "currency": args.get("currency", "USD"),
            "system": args.get("system", "ledgerlite"),
        }

    def run(self, args: dict[str, Any]) -> Observation:
        self.calls.append(args)
        return Observation(ok=True, summary=f"scheduled payment for {args['invoice_id']}")


class BrokenTool(Tool):
    name = "broken.tool"
    description = "Always fails."
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, error: Exception) -> None:
        self.error = error

    def run(self, args: dict[str, Any]) -> Observation:
        raise self.error


def policies():
    return load_company_context(ROOT / "company").policies


def test_tool_outside_the_allowlist_is_rejected() -> None:
    tool = RecordingTool()
    registry = ToolRegistry([tool], allowlist=["files.read"])

    observation = registry.invoke("erp.schedule_payment", {"invoice_id": "INV-1"})

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert "allowlist" in observation.summary
    assert observation.data["allowed"] == ["files.read"]
    assert tool.calls == []


def test_allowlisted_tool_is_invoked() -> None:
    tool = RecordingTool()
    registry = ToolRegistry([tool], allowlist=["erp.schedule_payment"])

    observation = registry.invoke("erp.schedule_payment", {"invoice_id": "INV-1"})

    assert observation.ok
    assert tool.calls == [{"invoice_id": "INV-1"}]


def test_allowlisted_but_unregistered_tool_is_not_found() -> None:
    registry = ToolRegistry([], allowlist=["files.read"])

    observation = registry.invoke("files.read", {"path": "a.txt"})

    assert not observation.ok
    assert observation.error_kind == "not_found"
    assert "files.read" in observation.summary


def test_forbidden_action_returns_a_policy_observation_without_a_side_effect() -> None:
    tool = RecordingTool()
    gate = PolicyGate(policies())
    registry = ToolRegistry([tool], allowlist=["erp.schedule_payment"], gate=gate)

    observation = registry.invoke(
        "erp.schedule_payment",
        {
            "invoice_id": "INV-1",
            "vendor_status": "blocked",
            "amount_usd": 300,
            "currency": "USD",
            "system": "ledgerlite",
        },
    )

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert observation.data["outcome"] == "forbid"
    assert observation.data["policy"] == "action-rules"
    assert observation.data["rule"] == "no-payment-to-blocked-vendor"
    assert tool.calls == []


def test_action_above_the_spend_limit_requires_approval_and_does_not_run() -> None:
    tool = RecordingTool()
    gate = PolicyGate(policies())
    registry = ToolRegistry([tool], allowlist=["erp.schedule_payment"], gate=gate)

    observation = registry.invoke(
        "erp.schedule_payment",
        {
            "invoice_id": "INV-1",
            "vendor_status": "active",
            "amount_usd": 20_000,
            "currency": "USD",
            "system": "ledgerlite",
        },
    )

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert observation.data["outcome"] == "require_approval"
    assert observation.data["approval_required"] is True
    assert observation.data["policy"] == "spend-limits"
    assert tool.calls == []


def test_allowed_action_passes_the_gate_and_runs() -> None:
    tool = RecordingTool()
    gate = PolicyGate(policies())
    registry = ToolRegistry([tool], allowlist=["erp.schedule_payment"], gate=gate)

    observation = registry.invoke(
        "erp.schedule_payment",
        {
            "invoice_id": "INV-1",
            "vendor_status": "active",
            "amount_usd": 300,
            "currency": "USD",
            "system": "ledgerlite",
        },
    )

    assert observation.ok
    assert tool.calls == [
        {
            "invoice_id": "INV-1",
            "vendor_status": "active",
            "amount_usd": 300,
            "currency": "USD",
            "system": "ledgerlite",
        }
    ]


def test_specs_only_include_allowlisted_registered_tools() -> None:
    tool = RecordingTool()
    extra = BrokenTool(ValueError("nope"))
    registry = ToolRegistry([tool, extra], allowlist=["erp.schedule_payment"])

    specs = registry.specs()

    assert registry.allowlist == ["erp.schedule_payment"]
    assert [spec["function"]["name"] for spec in specs] == ["erp.schedule_payment"]
    function = specs[0]["function"]
    assert function["description"] == tool.description
    assert function["parameters"] is tool.parameters


def test_registry_can_be_built_from_a_task_pack() -> None:
    from verirun.context.task_pack import load_task_pack

    task_pack = load_task_pack(ROOT / "tasks" / "invoice-processing.yaml")
    registry = ToolRegistry.from_task_pack(
        [RecordingTool()], task_pack, gate=PolicyGate(policies())
    )

    assert registry.allowlist == task_pack.tools
    assert registry.invoke("erp.schedule_payment", {"invoice_id": "INV-1"}).ok
    assert registry.invoke("files.read", {"path": "a.txt"}).error_kind == "not_found"


def test_declared_parameters_are_valid_json_schemas() -> None:
    tool = RecordingTool()

    assert json.loads(json.dumps(tool.parameters)) == tool.parameters
    assert tool.parameters["type"] == "object"


def test_tool_errors_map_to_observations_with_their_kind() -> None:
    registry = ToolRegistry(
        [BrokenTool(ToolError("transient", "the UI blinked"))],
        allowlist=["broken.tool"],
    )
    unexpected = ToolRegistry(
        [BrokenTool(RuntimeError("boom"))],
        allowlist=["broken.tool"],
    )

    transient = registry.invoke("broken.tool")
    unknown = unexpected.invoke("broken.tool")

    assert (transient.ok, transient.error_kind, transient.summary) == (
        False,
        "transient",
        "the UI blinked",
    )
    assert (unknown.ok, unknown.error_kind) == (False, "unknown")
    assert "RuntimeError" in unknown.summary


def test_gate_ignores_tools_without_a_policy_action() -> None:
    tool = RecordingTool()
    tool.action = None
    gate = PolicyGate(policies())
    registry = ToolRegistry([tool], allowlist=["erp.schedule_payment"], gate=gate)

    observation = registry.invoke(
        "erp.schedule_payment",
        {"invoice_id": "INV-1", "vendor_status": "blocked"},
    )

    assert observation.ok
    assert tool.calls


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate tool"):
        ToolRegistry([RecordingTool(), RecordingTool()], allowlist=["erp.schedule_payment"])


class LedgerPaymentTool(Tool):
    """A payment tool that reads vendor status and would write to the mock ERP."""

    name = "erp.schedule_payment"
    description = "Schedule a payment in LedgerLite."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ["invoice_id"],
        "additionalProperties": False,
    }
    action = "payment.schedule"

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        with closing(db.connect(self.db_path)) as conn:
            vendor = conn.execute(
                "SELECT status FROM vendors WHERE id = ?", (args.get("vendor_id"),)
            ).fetchone()
        return {
            "vendor_status": vendor["status"] if vendor else "unknown",
            "amount_usd": args.get("amount_usd", 0),
            "currency": args.get("currency", "USD"),
            "system": args.get("system", "ledgerlite"),
        }

    def run(self, args: dict[str, Any]) -> Observation:
        with closing(db.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO payments
                    (id, invoice_id, amount_cents, currency, scheduled_for, status, scenario)
                VALUES (?, ?, ?, 'USD', '2026-10-01', 'scheduled', 'test')
                """,
                (args["payment_id"], args["invoice_id"], args["amount_cents"]),
            )
        return Observation(ok=True, summary=f"scheduled {args['payment_id']}")


def payment_count(db_path: Path) -> int:
    with closing(db.connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0])


def test_forbidden_payment_leaves_the_mock_state_unchanged(ledgerlite_db: Path) -> None:
    registry = ToolRegistry(
        [LedgerPaymentTool(ledgerlite_db)],
        allowlist=["erp.schedule_payment"],
        gate=PolicyGate(policies()),
    )
    before = payment_count(ledgerlite_db)

    observation = registry.invoke(
        "erp.schedule_payment",
        {
            "payment_id": "PAY-9999",
            "invoice_id": "INV-3001",
            "vendor_id": "V-1006",
            "amount_usd": 300,
            "amount_cents": 30000,
            "currency": "USD",
            "system": "ledgerlite",
        },
    )

    assert not observation.ok
    assert observation.error_kind == "policy"
    assert observation.data["rule"] == "no-payment-to-blocked-vendor"
    assert payment_count(ledgerlite_db) == before
