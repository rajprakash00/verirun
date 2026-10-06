"""Acceptance tests for the vendor onboarding Task Pack on the unchanged engine.

The vendor request comes from MailDesk, the vendor is created in LedgerLite
behind an approval gate, the tax form is archived, and the Verifier checks real
state. The LLM is scripted, so the whole Run is deterministic and offline while
every effect touches the real Mock Suite. The same CLI command runs it as the
invoice task.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from mocks.ledgerlite import db as ledgerlite
from tests.support import ScriptedClient, run_settings, text_turn, tool_turn
from verirun.cli import main
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.web import create_app

CASCADE = {
    "name": "Cascade Fabrication LLC",
    "tax_id": "TAX-2001",
    "address": "4820 Foundry Way, Portland, OR 97210",
    "email": "accounts@cascade-fabrication.example",
}
CASCADE_FORM = "documents/vendor-onboarding/cascade-fabrication-w9.pdf"

VENDOR_WORK_ORDER = {
    "sop": "vendor-onboarding",
    "goal": "Onboard Cascade Fabrication LLC as a new supplier",
    "assumptions": ["The procurement mailbox holds the vendor request in scope"],
    "systems": ["maildesk", "ledgerlite", "files"],
    "policies": ["vendor-management"],
    "approval_gates": [
        {
            "id": "vendor-creation",
            "description": "Creating a vendor waits for a human decision",
            "policy": "vendor-management",
        }
    ],
    "success_criteria": ["Cascade Fabrication LLC exists in LedgerLite exactly once"],
    "open_questions": [],
}

VENDOR_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the onboarding email and its tax form",
            "allowed_tools": ["mail.read", "files.read"],
            "done_criterion": "The vendor legal name, tax id, and email are extracted",
        },
        {
            "id": "step-2",
            "goal": "Check LedgerLite for an existing vendor",
            "allowed_tools": ["erp.list_vendors"],
            "done_criterion": "No vendor with the same name or tax id exists",
        },
        {
            "id": "step-3",
            "goal": "Create the vendor in LedgerLite",
            "allowed_tools": ["erp.create_vendor"],
            "done_criterion": "The vendor exists in LedgerLite",
        },
        {
            "id": "step-4",
            "goal": "Archive the tax form",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The tax form is in the archive folder",
        },
    ]
}


def vendor_work_order(**overrides) -> str:
    return json.dumps({**VENDOR_WORK_ORDER, **overrides})


def happy_script() -> ScriptedClient:
    return ScriptedClient(
        [
            vendor_work_order(),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7009"}, call_id="c1"),
            text_turn("Read the W-9 attachment for Cascade Fabrication LLC."),
            tool_turn("erp.list_vendors", {}, call_id="c2"),
            text_turn("No existing vendor matches Cascade Fabrication LLC."),
            tool_turn("erp.create_vendor", CASCADE, call_id="c3"),
            # Consumed after the human approval resumes the Run.
            text_turn("Vendor created."),
            tool_turn(
                "files.archive",
                {"path": CASCADE_FORM, "directory": "archive"},
                call_id="c4",
            ),
            text_turn("Tax form archived."),
        ]
    )


def test_a_vendor_onboarding_request_completes_verified_after_approval(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys,
) -> None:
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    script = happy_script()

    code = main(
        [
            "run",
            "Onboard Cascade Fabrication LLC from the procurement mailbox",
            "--task",
            "vendor-onboarding",
        ],
        settings=settings,
        client=script,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "awaiting_approval" in output
    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.AWAITING_APPROVAL
    request = store.open_approval(run.id)
    assert request is not None
    assert request.tool == "erp.create_vendor"
    assert request.action == "vendor.create"
    assert request.arguments == CASCADE
    assert request.policy == "vendor-management"
    assert request.rule == "vendor-creation-needs-approval"
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        assert conn.execute("SELECT * FROM vendors WHERE tax_id = 'TAX-2001'").fetchall() == []

    client = TestClient(create_app(settings, client_factory=lambda _run_id: script))
    response = client.post(f"/approvals/{request.id}/approve", follow_redirects=False)

    assert response.status_code == 303
    run = store.get_run(run.id)
    assert run.state is RunState.COMPLETED
    assert store.get_approval(request.id).executed_at is not None
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        vendors = conn.execute("SELECT * FROM vendors WHERE tax_id = 'TAX-2001'").fetchall()
    assert len(vendors) == 1
    assert vendors[0]["id"] == "V-1009"
    assert vendors[0]["name"] == CASCADE["name"]
    assert vendors[0]["email"] == CASCADE["email"]
    assert vendors[0]["address"] == CASCADE["address"]
    assert vendors[0]["status"] == "active"
    assert (maildesk_state.shared_root / "archive" / "cascade-fabrication-w9.pdf").is_file()
    assert not (maildesk_state.shared_root / CASCADE_FORM).exists()

    evidence = json.loads(
        (settings.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["result"] == "verified"
    assert evidence["verified"] is True
    checks = {check["id"]: check for check in evidence["verification"]["checks"]}
    assert checks["vendor-created"]["ok"] is True
    assert checks["tax-document-archived"]["ok"] is True


def test_a_duplicate_vendor_is_caught_before_creation(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys,
) -> None:
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    script = ScriptedClient(
        [
            vendor_work_order(goal="Onboard Apex Office Supplies as a new supplier"),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7010"}, call_id="c1"),
            tool_turn(
                "files.read",
                {"path": "documents/vendor-onboarding/apex-office-supplies-w9.pdf"},
                call_id="c2",
            ),
            text_turn("Extracted Apex Office Supplies with tax id TAX-1002."),
            tool_turn("erp.list_vendors", {}, call_id="c3"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "A vendor with tax id TAX-1002 already exists as V-1002",
                    "question": (
                        "Apex Office Supplies is already vendor V-1002. Should the Run "
                        "update the existing vendor instead of creating a duplicate?"
                    ),
                },
                call_id="c4",
            ),
        ]
    )

    code = main(
        [
            "run",
            "Onboard Apex Office Supplies from the procurement mailbox",
            "--task",
            "vendor-onboarding",
        ],
        settings=settings,
        client=script,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "needs_human" in output
    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.NEEDS_HUMAN
    escalation = store.open_escalation(run.id)
    assert escalation is not None
    assert "V-1002" in escalation.question
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        vendors = conn.execute("SELECT * FROM vendors WHERE tax_id = 'TAX-1002'").fetchall()
    assert len(vendors) == 1
    assert vendors[0]["id"] == "V-1002"
    assert [entry.action for entry in store.list_journal(run.id)] == []


def test_a_missing_tax_form_escalates(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys,
) -> None:
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    script = ScriptedClient(
        [
            vendor_work_order(goal="Onboard Meridian Plastics as a new supplier"),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7011"}, call_id="c1"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "The onboarding email for Meridian Plastics has no tax form",
                    "question": "Which tax form should be used for Meridian Plastics?",
                },
                call_id="c2",
            ),
        ]
    )

    code = main(
        [
            "run",
            "Onboard Meridian Plastics from the procurement mailbox",
            "--task",
            "vendor-onboarding",
        ],
        settings=settings,
        client=script,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "needs_human" in output
    assert "Which tax form should be used for Meridian Plastics?" in output
    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.NEEDS_HUMAN
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        assert conn.execute("SELECT * FROM vendors WHERE tax_id = 'TAX-2002'").fetchall() == []
    assert [entry.action for entry in store.list_journal(run.id)] == []
