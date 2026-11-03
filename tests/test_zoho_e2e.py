"""End to end: a fixture GST invoice matched against recorded Zoho Books India reads.

The executor reads the mailbox and the Zoho adapter, extracts the GST invoice,
and archives the source. The Verifier gets a separate read-only Zoho object,
so its vendor, duplicate, and purchase-order checks cannot travel through the
executor's write handle. No purchase bill is created.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from tests.india_fixture import (
    INVOICE_NUMBER,
    PDF_FILENAME,
    extraction_json,
    write_invoice_pdf,
)
from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from tests.zoho_fixture import PO_ID as ZOHO_PO_ID
from tests.zoho_fixture import VENDOR_ID, fixture, recorded
from verirun.adapters import WatchedFolderSource
from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoAccountingSystem,
    ZohoBooksClient,
    ZohoOAuthConfig,
    ZohoTokenManager,
    ZohoTokenStore,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry

REQUEST = "Match the GST invoice in the AP mailbox against Zoho Books"
WORK_ORDER = {
    "sop": "india-gst-extraction",
    "goal": "Match the GST invoice against Zoho Books and archive its source",
    "assumptions": ["The Zoho Books vendor and purchase order are the source of truth"],
    "systems": ["ledgerlite", "files"],
    "policies": [],
    "approval_gates": [],
    "success_criteria": [
        "The invoice matches a Zoho Books vendor and purchase order",
        "No existing bill has the same number",
        "The source invoice is archived in processed",
    ],
    "open_questions": [],
}
PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and extract its India GST fields",
            "allowed_tools": ["mail.read", "invoice.extract"],
            "done_criterion": "Every field has a confidence and a citation",
        },
        {
            "id": "step-2",
            "goal": "Match the supplier and purchase order in Zoho Books",
            "allowed_tools": ["erp.list_vendors", "erp.list_purchase_orders"],
            "done_criterion": "The vendor and purchase order are identified",
        },
        {
            "id": "step-3",
            "goal": "Archive the source invoice",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}


class SpyAccounting(ZohoAccountingSystem):
    """The executor's write handle, recording which reads it served."""

    def __init__(self, reads: ZohoAccountingReads) -> None:
        super().__init__(reads)
        self.calls: list[str] = []

    def list_vendors(self, *, status: str | None = None):
        self.calls.append("list_vendors")
        return super().list_vendors(status=status)

    def find_vendors(self, *, name: str | None = None, tax_id: str | None = None):
        self.calls.append("find_vendors")
        return super().find_vendors(name=name, tax_id=tax_id)

    def list_purchase_orders(
        self, *, vendor_id: str | None = None, status: str | None = None
    ):
        self.calls.append("list_purchase_orders")
        return super().list_purchase_orders(vendor_id=vendor_id, status=status)

    def list_bills(
        self,
        *,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
    ):
        self.calls.append("list_bills")
        return super().list_bills(number=number, vendor_id=vendor_id, status=status)


def zoho_reads(tmp_path: Path, handler=recorded) -> tuple[ZohoAccountingReads, ZohoTokenStore]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    store = ZohoTokenStore(tmp_path / "zoho-read-token.enc", "operator-secret")
    oauth = ZohoOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="zoho-refresh-token",
        accounts_domain="https://accounts.zoho.in",
    )
    manager = ZohoTokenManager(oauth, store, http=http, sleep=lambda seconds: None)
    client = ZohoBooksClient(
        token_manager=manager,
        organization_id="6000000000001",
        base_url="https://www.zohoapis.in/books/v3",
        http=http,
        sleep=lambda seconds: None,
    )
    return ZohoAccountingReads(client), store


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
        mail_state_db=tmp_path / "mail-index.db",
    )


def script(message_id: str, attachment_path: str) -> ScriptedClient:
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(PLAN),
            tool_turn("mail.read", {"message_id": message_id}, call_id="c1"),
            tool_turn("invoice.extract", {"path": attachment_path}, call_id="c2"),
            text_turn(extraction_json()),
            text_turn("Extracted the invoice with citations."),
            tool_turn("erp.list_vendors", {"status": "active"}, call_id="c3"),
            tool_turn("erp.list_purchase_orders", {"vendor_id": VENDOR_ID}, call_id="c4"),
            text_turn("The vendor and purchase order match."),
            tool_turn(
                "files.archive",
                {"path": attachment_path, "directory": "processed"},
                call_id="c5",
            ),
            text_turn("Archived the source invoice."),
        ]
    )


def run_scenario(
    tmp_path: Path, *, handler=recorded, run_id: str = "RUN-ZOHO-0001"
) -> tuple[Any, dict, SpyAccounting, ZohoTokenStore]:
    watch = tmp_path / "watch"
    watch.mkdir()
    write_invoice_pdf(watch / PDF_FILENAME)
    chosen = settings(tmp_path)
    mail = WatchedFolderSource(watch, chosen.shared_dir, chosen.mail_state_db)
    assert mail.sync() == 1
    summary = mail.list_messages()[0]
    message = mail.read_message(summary.id)
    attachment = message.attachments[0]

    reads, token_store = zoho_reads(tmp_path, handler)
    write = SpyAccounting(reads)
    backends = Backends(mail=mail, accounting=write, reads=reads)
    assert backends.verifier_accounting is reads
    context = load_company_context(chosen.company_dir)
    pack = load_task_pack(ROOT / "tasks" / "invoice-processing-in.yaml")
    store = RunStore(chosen.run_db)
    llm = script(summary.id, attachment.path)
    registry = build_registry(chosen, context, pack, llm, backends=backends)

    run = run_task(
        REQUEST,
        pack,
        context,
        llm,
        store,
        registry,
        accounting=backends.verifier_accounting,
        shared_root=mail.document_root,
        evidence_root=chosen.run_db.parent,
        run_id=run_id,
    )
    evidence = json.loads(
        (chosen.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )
    return run, evidence, write, token_store


def duplicate_bill(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/bills"):
        payload = fixture("bills-page-1.json")
        payload["bills"].append(
            {
                "bill_id": "4600000000209",
                "bill_number": INVOICE_NUMBER,
                "vendor_id": VENDOR_ID,
                "vendor_name": "Paper Trail Supplies Pvt. Ltd.",
                "total": "11800.00",
                "currency_code": "INR",
                "status": "open",
                "date": "2026-09-05",
            }
        )
        return httpx.Response(200, json=payload)
    return recorded(request)


def test_a_fixture_invoice_is_matched_against_recorded_zoho_books(
    tmp_path: Path,
) -> None:
    run, evidence, write, token_store = run_scenario(tmp_path)

    assert run.state is RunState.COMPLETED, run.error
    checks = {check["id"]: check for check in evidence["verification"]["checks"]}
    assert evidence["verification"]["passed"] is True
    match = checks["accounting-invoice-matches"]
    assert match["ok"] is True
    assert match["evidence"] == [
        f"accounting:vendor:{VENDOR_ID}",
        f"accounting:no-bill:{VENDOR_ID}:{INVOICE_NUMBER}",
        f"accounting:purchaseorder:{ZOHO_PO_ID}",
    ]
    assert checks["source-archived"]["ok"] is True

    # The Verifier's own find_bills and find_vendors reads never touched the
    # executor's write handle; only the two executor tool calls did.
    assert write.calls == ["list_vendors", "list_purchase_orders"]

    raw = token_store.path.read_bytes()
    assert b"zoho-access-token" not in raw
    assert b"zoho-refresh-token" not in raw
    assert not any(item["tool"] == "erp.file_invoice" for item in evidence["observations"])


def test_a_duplicate_bill_in_zoho_fails_with_the_existing_bill_as_evidence(
    tmp_path: Path,
) -> None:
    run, evidence, write, _ = run_scenario(
        tmp_path, handler=duplicate_bill, run_id="RUN-ZOHO-0002"
    )

    assert run.state is RunState.FAILED
    checks = {check["id"]: check for check in evidence["verification"]["checks"]}
    match = checks["accounting-invoice-matches"]
    assert match["ok"] is False
    assert "4600000000209" in match["detail"]
    assert "accounting:bill:4600000000209" in match["evidence"]
    assert write.calls == ["list_vendors", "list_purchase_orders"]
