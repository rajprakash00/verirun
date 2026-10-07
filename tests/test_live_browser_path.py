"""The live path registers the browser tools: CLI runs and dashboard resumes.

The runtime builds one registry for the CLI and the dashboard, with a browser
session scoped to the Run. These tests assert the browser tools are present on
that shared path and that a browser Step executes against the Mock Suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import (
    ROOT,
    WORK_ORDER,
    ScriptedClient,
    run_settings,
    text_turn,
    tool_turn,
)
from verirun.cli import main
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import build_registry, live_registry
from verirun.tools import BrowserSession
from verirun.web import create_app

BROWSER_TOOLS = {
    "browser.navigate",
    "browser.snapshot",
    "browser.click",
    "browser.type",
    "browser.select",
    "browser.extract",
    "browser.screenshot",
}

LIVE_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and check LedgerLite in the browser",
            "allowed_tools": ["mail.read", "browser.navigate", "browser.snapshot"],
            "done_criterion": "NW-2026-001 is located and LedgerLite is open in the browser",
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


@pytest.fixture(scope="module")
def chromium(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Skip the live browser tests when Chromium is not installed.

    The check starts and closes its own session before yielding: the live path
    starts another one, and the sync Playwright API allows only one session per
    thread at a time.
    """
    session = BrowserSession(artifact_dir=tmp_path_factory.mktemp("chromium") / "artifacts")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - report missing system dependencies as a skip
        pytest.skip(
            f"Chromium is unavailable ({exc}); "
            "run `uv run playwright install --with-deps chromium`"
        )
    finally:
        session.close()
    yield


RESUME_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Summit Industrial invoice",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "SI-2026-550 is filed against PO-2005 and GR-2505",
        },
        {
            "id": "step-2",
            "goal": "Prepare the payment and check the ERP record in the browser",
            "allowed_tools": [
                "erp.schedule_payment",
                "browser.navigate",
                "browser.snapshot",
            ],
            "done_criterion": (
                "The payment is scheduled once a human approves it and LedgerLite is open"
            ),
        },
        {
            "id": "step-3",
            "goal": "Archive the source invoice",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}


def resume_script(ledgerlite_url: str) -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(RESUME_PLAN),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "SI-2026-550",
                    "vendor_id": "V-1005",
                    "amount": 12500.00,
                    "po_id": "PO-2005",
                    "gr_id": "GR-2505",
                },
                call_id="c1",
            ),
            text_turn("Filed SI-2026-550."),
            tool_turn("erp.schedule_payment", {"invoice_id": "INV-3003"}, call_id="c2"),
            # Consumed after the human approval resumes the Run.
            tool_turn(
                "browser.navigate", {"url": f"{ledgerlite_url}/invoices"}, call_id="c3"
            ),
            tool_turn("browser.snapshot", call_id="c4"),
            text_turn("Payment scheduled and the ERP record checked."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/SI-2026-550.pdf", "directory": "processed"},
                call_id="c5",
            ),
            text_turn("Archived."),
        ]
    )


def live_script(ledgerlite_url: str) -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(LIVE_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7001"}, call_id="c1"),
            tool_turn("browser.navigate", {"url": f"{ledgerlite_url}/invoices"}, call_id="c2"),
            tool_turn("browser.snapshot", call_id="c3"),
            text_turn("Located NW-2026-001 and checked LedgerLite."),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c4"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, call_id="c5"),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "NW-2026-001",
                    "vendor_id": "V-1001",
                    "amount": 1250.00,
                    "po_id": "PO-2001",
                    "gr_id": "GR-2501",
                },
                call_id="c6",
            ),
            text_turn("Filed as INV-3003."),
            tool_turn(
                "erp.schedule_payment",
                {"invoice_id": "INV-3003", "scheduled_for": "2026-10-05"},
                call_id="c7",
            ),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/NW-2026-001.pdf", "directory": "processed"},
                call_id="c8",
            ),
            text_turn("Payment scheduled and source archived."),
        ]
    )


def test_the_live_registry_registers_the_browser_tools(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
    )
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")

    with live_registry(settings, context, task_pack, ScriptedClient([]), "RUN-0001") as registry:
        available = {tool.name for tool in registry.available()}
        specs = {spec["function"]["name"] for spec in registry.specs()}

    assert BROWSER_TOOLS <= available
    assert BROWSER_TOOLS <= specs


def test_cli_run_executes_a_browser_step(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    ledgerlite_server: str,
    chromium: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = run_settings(
        tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state
    )

    code = main(
        ["run", "Process the invoices in the AP mailbox", "--task", "invoice-processing"],
        settings=settings,
        client=live_script(ledgerlite_server),
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "finished in state completed" in output

    store = RunStore(settings.run_db)
    run = store.get_run(store.list_runs()[0].id)
    assert run.state is RunState.COMPLETED
    navigations = [
        observation
        for observation in store.list_observations(run.id)
        if observation.tool == "browser.navigate"
    ]
    assert [observation.ok for observation in navigations] == [True]
    assert navigations[0].data["url"] == f"{ledgerlite_server}/invoices"


def test_approving_over_http_resumes_with_a_browser_step(
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    ledgerlite_server: str,
    chromium: None,
) -> None:
    settings = run_settings(
        tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state
    )
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    store = RunStore(settings.run_db)
    script = resume_script(ledgerlite_server)
    run = run_task(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        script,
        store,
        build_registry(settings, context, task_pack, script),
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
        evidence_root=settings.run_db.parent,
    )
    request = store.open_approval(run.id)
    assert request is not None
    client = TestClient(create_app(settings, client_factory=lambda _run_id: script))

    response = client.post(f"/approvals/{request.id}/approve", follow_redirects=False)

    assert response.status_code == 303
    resumed = store.get_run(run.id)
    assert resumed.state is RunState.COMPLETED
    navigations = [
        observation
        for observation in store.list_observations(run.id)
        if observation.tool == "browser.navigate"
    ]
    assert [observation.ok for observation in navigations] == [True]
    assert navigations[0].data["url"] == f"{ledgerlite_server}/invoices"
