"""End to end: fixture IMAP mailbox to a reviewable India GST Evidence Pack.

No Zoho credentials, no Mock Suite mailbox, no browser. The Run connects to a
local IMAP server, ingests the fixture invoice idempotently, extracts its GST
fields with citations and confidences, runs the GST validations, matches the
vendor and purchase order against the in-memory accounting fake, archives the
source, and writes an Evidence Pack.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from tests.imap_server import ImapTestServer
from tests.india_fixture import (
    GSTIN,
    INVOICE_NUMBER,
    PO_ID,
    SUPPLIER,
    TOTAL_PAISE,
    extraction_json,
    write_fixture_mailbox,
)
from tests.support import ROOT, ScriptedClient, text_turn, tool_turn
from verirun.adapters import (
    ImapSource,
    InMemoryAccountingSystem,
    PurchaseOrder,
    Vendor,
)
from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.runs.store import RunStore
from verirun.runtime import Backends, build_registry

CREATED_AT = "2026-09-01T09:00:00+00:00"
REQUEST = "Process the tax invoice in the AP mailbox"
WORK_ORDER = {
    "sop": "india-gst-extraction",
    "goal": "Extract the GST invoice and archive its source",
    "assumptions": ["The AP mailbox is the source of truth"],
    "systems": ["maildesk", "ledgerlite", "files"],
    "policies": [],
    "approval_gates": [],
    "success_criteria": [
        "The invoice fields are extracted with citations and GST validations",
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
            "goal": "Match the supplier GSTIN and the purchase order",
            "allowed_tools": [
                "erp.list_vendors",
                "erp.list_purchase_orders",
                "erp.get_purchase_order",
            ],
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


def india_pack():
    return load_task_pack(ROOT / "tasks" / "invoice-processing-in.yaml")


def accounting_fake() -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id="V-IN-1001",
                name=SUPPLIER,
                tax_id=GSTIN,
                email="accounts@papertrail.example",
                address="14 Market Road, Bengaluru",
                status="active",
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=[
            PurchaseOrder(
                id=PO_ID,
                vendor_id="V-IN-1001",
                amount_cents=TOTAL_PAISE,
                currency="INR",
                status="open",
                issued_at=CREATED_AT,
            )
        ],
    )


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=tmp_path / "shared",
        mail_state_db=tmp_path / "mail-index.db",
    )


def test_fixture_mailbox_becomes_a_verified_evidence_pack(tmp_path: Path) -> None:
    mailbox = write_fixture_mailbox(tmp_path / "fixture")
    chosen = settings(tmp_path)
    with ImapTestServer() as server:
        server.add_mailbox(mailbox[0].parent)
        source = ImapSource(
            host=server.host,
            port=server.port,
            username="ap@company.example",
            password="app-password",
            document_root=chosen.shared_dir,
            state_db=chosen.mail_state_db,
            use_ssl=False,
        )
        assert source.sync() == 1
        assert source.sync() == 0
        summary = source.list_messages()[0]
        message = source.read_message(summary.id)
        attachment = message.attachments[0]
        pdf_bytes = source.read_attachment(summary.id, attachment.id)

        script = ScriptedClient(
            [
                json.dumps(WORK_ORDER),
                json.dumps(PLAN),
                tool_turn("mail.read", {"message_id": summary.id}, call_id="c1"),
                tool_turn("invoice.extract", {"path": attachment.path}, call_id="c2"),
                text_turn(extraction_json()),
                text_turn("Extracted the invoice with citations."),
                tool_turn("erp.list_vendors", {"status": "active"}, call_id="c3"),
                tool_turn(
                    "erp.list_purchase_orders", {"vendor_id": "V-IN-1001"}, call_id="c4"
                ),
                tool_turn("erp.get_purchase_order", {"po_id": PO_ID}, call_id="c5"),
                text_turn("The supplier GSTIN and purchase order match."),
                tool_turn(
                    "files.archive",
                    {"path": attachment.path, "directory": "processed"},
                    call_id="c6",
                ),
                text_turn("Archived the source invoice."),
            ]
        )
        context = load_company_context(chosen.company_dir)
        pack = india_pack()
        backends = Backends(mail=source, accounting=accounting_fake())
        store = RunStore(chosen.run_db)
        registry = build_registry(
            chosen, context, pack, script, backends=backends
        )
        run = run_task(
            REQUEST,
            pack,
            context,
            script,
            store,
            registry,
            accounting=backends.verifier_accounting,
            shared_root=source.document_root,
            evidence_root=chosen.run_db.parent,
            run_id="RUN-IN-0001",
        )

    assert run.state is RunState.COMPLETED, run.error
    evidence = json.loads(
        (chosen.run_db.parent / run.id / "evidence.json").read_text(encoding="utf-8")
    )

    assert evidence["result"] == "verified"
    assert evidence["verification"]["passed"] is True
    assert [check["id"] for check in evidence["verification"]["checks"]] == [
        "source-archived"
    ]

    source_record = evidence["sources"][0]
    assert source_record["message_id"] == summary.id
    assert source_record["subject"].startswith(f"Tax invoice {INVOICE_NUMBER}")
    recorded_attachment = source_record["attachments"][0]
    digest = sha256(pdf_bytes).hexdigest()
    assert recorded_attachment["id"] == digest
    assert recorded_attachment["path"].startswith("attachments/")

    extraction = next(
        item for item in evidence["extractions"] if item["schema"] == "india-gst"
    )
    assert extraction["ok"] is True
    fields = extraction["fields"]
    assert fields["supplier_name"]["value"] == SUPPLIER
    assert fields["supplier_gstin"]["value"] == GSTIN
    assert fields["supplier_gstin"]["citation"] == f"GSTIN: {GSTIN}"
    assert fields["invoice_number"]["value"] == INVOICE_NUMBER
    assert fields["invoice_date"]["value"] == "2026-09-05"
    assert fields["taxable_amount_paise"]["value"] == 1_000_000
    assert fields["cgst_paise"]["value"] == 90_000
    assert fields["sgst_paise"]["value"] == 90_000
    assert fields["igst_paise"]["value"] == 0
    assert fields["total_paise"]["value"] == TOTAL_PAISE
    assert fields["po_reference"]["value"] == PO_ID
    assert fields["currency"]["value"] == "INR"
    for field in fields.values():
        if field["value"] is not None:
            assert 0.0 <= field["confidence"] <= 1.0
            assert field["citation"]

    validations = {item["id"]: item for item in evidence["validations"]}
    assert set(validations) == {"gstin_checksum", "tax_sum", "tax_split"}
    assert all(item["ok"] for item in validations.values()), [
        item["detail"] for item in validations.values()
    ]
    assert validations["gstin_checksum"]["source"] == extraction["path"]

    archived = [item for item in evidence["artifacts"] if item.startswith("processed/")]
    assert archived == [f"processed/{attachment.filename}"]

    observations = {
        item["tool"]: item for item in evidence["observations"] if item["ok"]
    }
    vendors = observations["erp.list_vendors"]["data"]["vendors"]
    assert [(vendor["id"], vendor["tax_id"]) for vendor in vendors] == [
        ("V-IN-1001", GSTIN)
    ]
    orders = observations["erp.list_purchase_orders"]["data"]["purchase_orders"]
    assert [
        (order["id"], order["vendor_id"], order["amount_cents"], order["currency"])
        for order in orders
    ] == [(PO_ID, "V-IN-1001", TOTAL_PAISE, "INR")]
    order = observations["erp.get_purchase_order"]["data"]["purchase_order"]
    assert order["amount_cents"] == fields["total_paise"]["value"]
    assert script.replies == []
