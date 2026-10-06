"""The verified walking skeleton: MailDesk request -> LedgerLite -> archive -> verified.

The LLM is scripted, so the whole Run is deterministic and offline while every
effect and every verification check touches the real Mock Suite.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from mocks.ledgerlite import db as ledgerlite
from tests.support import ROOT, WORK_ORDER, ScriptedClient, text_turn, tool_turn
from verirun.cli import main
from verirun.config import Settings
from verirun.engine.states import RunState
from verirun.runs.store import RunStore

FILED_INVOICE_ID = "INV-3003"

WALK_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and locate its attachment",
            "allowed_tools": ["mail.read", "files.read"],
            "done_criterion": "The invoice NW-2026-001 is located in the shared file tree",
        },
        {
            "id": "step-2",
            "goal": "Validate and file the invoice in LedgerLite",
            "allowed_tools": [
                "erp.get_purchase_order",
                "erp.get_goods_receipt",
                "erp.file_invoice",
            ],
            "done_criterion": "The invoice is filed against PO-2001 and GR-2501",
        },
        {
            "id": "step-3",
            "goal": "Schedule the payment and archive the source",
            "allowed_tools": ["erp.schedule_payment", "files.archive"],
            "done_criterion": "The payment is scheduled and the source is in processed",
        },
    ]
}


def skeleton_settings(tmp_path: Path, ledgerlite_db: Path, maildesk_state) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=maildesk_state.shared_root,
        mail_db=maildesk_state.db_path,
        erp_db=ledgerlite_db,
    )


def happy_script() -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(WALK_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7001"}, call_id="c1"),
            text_turn("Located NW-2026-001 in the batch email."),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c2"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, call_id="c3"),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "NW-2026-001",
                    "vendor_id": "V-1001",
                    "amount": 1250.00,
                    "po_id": "PO-2001",
                    "gr_id": "GR-2501",
                },
                call_id="c4",
            ),
            text_turn(f"Filed as {FILED_INVOICE_ID}."),
            tool_turn(
                "erp.schedule_payment",
                {"invoice_id": FILED_INVOICE_ID, "scheduled_for": "2026-10-05"},
                call_id="c5",
            ),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/NW-2026-001.pdf", "directory": "processed"},
                call_id="c6",
            ),
            text_turn("Payment scheduled and source archived."),
        ]
    )


def test_invoice_processing_completes_and_reports_verified(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = skeleton_settings(tmp_path, ledgerlite_db, maildesk_state)

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=happy_script(),
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "finished in state completed" in output
    assert "[pass] invoice-filed" in output
    assert "[pass] payment-scheduled" in output
    assert "[pass] source-archived" in output

    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.COMPLETED
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE number = 'NW-2026-001' AND scenario = 'filed'"
        ).fetchone()
        payments = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchall()
    assert invoice["amount_cents"] == 125_000
    assert invoice["vendor_id"] == "V-1001"
    assert len(payments) == 1
    assert payments[0]["status"] == "scheduled"
    assert (maildesk_state.shared_root / "processed" / "NW-2026-001.pdf").is_file()

    evidence_path = settings.run_db.parent / run.id / "evidence.json"
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["result"] == "verified"
    assert evidence["verified"] is True
    assert evidence["verification"]["passed"] is True
    assert all(check["ok"] for check in evidence["verification"]["checks"])
    assert "processed/NW-2026-001.pdf" in evidence["artifacts"]
    assert evidence["steps_used"] == 6
    assert evidence["cost_usd"] >= 0


def test_skipping_the_erp_write_makes_verification_fail(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = skeleton_settings(tmp_path, ledgerlite_db, maildesk_state)
    script = ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(WALK_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7001"}, call_id="c1"),
            text_turn("Located NW-2026-001 in the batch email."),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c2"),
            text_turn("Validated the invoice but did not file it."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/NW-2026-001.pdf", "directory": "processed"},
                call_id="c3",
            ),
            text_turn("Archived the source without filing."),
        ]
    )

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=script,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] invoice-filed" in output
    assert "verification failed" in output

    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.FAILED
    assert run.error is not None and "invoice-filed" in run.error
    with closing(ledgerlite.connect(ledgerlite_db)) as conn:
        filed = conn.execute(
            "SELECT * FROM invoices WHERE number = 'NW-2026-001' AND scenario = 'filed'"
        ).fetchall()
    assert filed == []

    evidence = json.loads(
        (settings.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["result"] == "failed"
    assert evidence["verified"] is False
    checks = {check["id"]: check for check in evidence["verification"]["checks"]}
    assert not checks["invoice-filed"]["ok"]
    assert checks["source-archived"]["ok"]


def test_a_failed_resolve_still_leaves_an_evidence_pack(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = skeleton_settings(tmp_path, ledgerlite_db, maildesk_state)

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=ScriptedClient(["not json", "still not json"]),
    )

    assert code == 1
    assert "error:" in capsys.readouterr().err
    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.FAILED
    evidence = json.loads(
        (settings.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["result"] == "failed"
    assert evidence["verification"] is None
    assert "structured output" in evidence["error"]


def test_report_writes_the_evidence_pack_for_a_finished_run(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = skeleton_settings(tmp_path, ledgerlite_db, maildesk_state)
    main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=happy_script(),
    )
    capsys.readouterr()
    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    evidence_path = settings.run_db.parent / run.id / "evidence.json"
    evidence_path.unlink()

    code = main(["report", run.id], settings=settings)

    output = capsys.readouterr().out
    html_path = evidence_path.parent / "evidence.html"
    assert code == 0
    assert str(evidence_path) in output
    assert str(html_path) in output
    assert evidence_path.is_file()
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["result"] == "verified"
    assert "invoice-filed" in html_path.read_text(encoding="utf-8")
