"""Tests for the ERP tools against the seeded LedgerLite mock."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from company_operator.tools import ToolRegistry
from company_operator.tools.erp import build_erp_tools
from mocks.ledgerlite import db

ALL_TOOLS = [
    "erp.list_vendors",
    "erp.get_vendor",
    "erp.list_purchase_orders",
    "erp.get_purchase_order",
    "erp.get_goods_receipt",
    "erp.list_invoices",
    "erp.get_invoice",
    "erp.create_vendor",
    "erp.file_invoice",
    "erp.schedule_payment",
]


def registry(db_path: Path) -> ToolRegistry:
    return ToolRegistry(build_erp_tools(db_path), allowlist=ALL_TOOLS)


def fetch(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with closing(db.connect(db_path)) as conn:
        return conn.execute(sql, params).fetchall()


def test_reads_expose_the_seeded_ledgerlite_state(ledgerlite_db: Path) -> None:
    tools = registry(ledgerlite_db)

    vendors = tools.invoke("erp.list_vendors")
    vendor = tools.invoke("erp.get_vendor", {"vendor_id": "V-1001"})
    orders = tools.invoke("erp.list_purchase_orders", {"vendor_id": "V-1001"})
    order = tools.invoke("erp.get_purchase_order", {"po_id": "PO-2001"})
    receipt = tools.invoke("erp.get_goods_receipt", {"gr_id": "GR-2501"})
    invoices = tools.invoke("erp.list_invoices", {"number": "AQ-2026-014"})
    invoice = tools.invoke("erp.get_invoice", {"invoice_id": "INV-3002"})

    assert vendors.ok and len(vendors.data["vendors"]) == 9
    assert vendor.data["vendor"]["name"] == "Northwind Traders"
    assert [row["id"] for row in orders.data["purchase_orders"]] == ["PO-2001"]
    assert order.data["purchase_order"]["amount_cents"] == 125_000
    assert receipt.data["goods_receipt"]["po_id"] == "PO-2001"
    assert [row["id"] for row in invoices.data["invoices"]] == ["INV-3002"]
    assert invoice.data["invoice"]["status"] == "paid"


def test_reads_filter_and_report_missing_rows(ledgerlite_db: Path) -> None:
    tools = registry(ledgerlite_db)

    blocked = tools.invoke("erp.list_vendors", {"status": "blocked"})
    missing = tools.invoke("erp.get_vendor", {"vendor_id": "V-9999"})
    missing_po = tools.invoke("erp.get_purchase_order", {"po_id": "PO-9999"})

    assert [row["id"] for row in blocked.data["vendors"]] == ["V-1006"]
    assert missing.error_kind == "not_found"
    assert missing_po.error_kind == "not_found"


def test_file_invoice_writes_a_received_invoice_against_its_references(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    observation = tools.invoke(
        "erp.file_invoice",
        {
            "number": "NW-2026-001",
            "vendor_id": "V-1001",
            "amount": "1,250.00",
            "po_id": "PO-2001",
            "gr_id": "GR-2501",
        },
    )

    assert observation.ok
    assert observation.data["amount_cents"] == 125_000
    assert observation.data["status"] == "received"
    rows = fetch(
        ledgerlite_db,
        "SELECT * FROM invoices WHERE number = 'NW-2026-001' AND vendor_id = 'V-1001'",
    )
    assert len(rows) == 1
    assert rows[0]["id"] == observation.data["invoice_id"]
    assert rows[0]["po_id"] == "PO-2001"
    assert rows[0]["gr_id"] == "GR-2501"
    assert rows[0]["amount_cents"] == 125_000
    assert rows[0]["scenario"] == "filed"


def test_file_invoice_consumes_a_seeded_transient_failure_once(ledgerlite_db: Path) -> None:
    tools = registry(ledgerlite_db)
    arguments = {
        "number": "RS-2026-088",
        "vendor_id": "V-1007",
        "amount": 890.00,
        "po_id": "PO-2007",
        "gr_id": "GR-2507",
    }

    first = tools.invoke("erp.file_invoice", arguments)
    second = tools.invoke("erp.file_invoice", arguments)

    assert not first.ok
    assert first.error_kind == "transient"
    assert second.ok
    assert len(fetch(ledgerlite_db, "SELECT * FROM invoices WHERE number = 'RS-2026-088'")) == 1


def test_file_invoice_rejects_a_duplicate_or_an_unknown_vendor(ledgerlite_db: Path) -> None:
    tools = registry(ledgerlite_db)

    duplicate = tools.invoke(
        "erp.file_invoice",
        {"number": "AQ-2026-014", "vendor_id": "V-1002", "amount": 640.00},
    )
    unknown = tools.invoke(
        "erp.file_invoice",
        {"number": "XX-2026-001", "vendor_id": "V-9999", "amount": 10.00},
    )
    bad_amount = tools.invoke(
        "erp.file_invoice",
        {"number": "XX-2026-002", "vendor_id": "V-1001", "amount": "not money"},
    )

    assert duplicate.error_kind == "invalid"
    assert "already exists" in duplicate.summary
    assert unknown.error_kind == "not_found"
    assert bad_amount.error_kind == "invalid"
    assert fetch(ledgerlite_db, "SELECT * FROM invoices WHERE number = 'XX-2026-001'") == []


def test_file_invoice_reports_a_duplicate_with_the_existing_record(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    duplicate = tools.invoke(
        "erp.file_invoice",
        {"number": "AQ-2026-014", "vendor_id": "V-1002", "amount": 640.00},
    )

    assert duplicate.error_kind == "invalid"
    assert duplicate.data["duplicate"] is True
    assert duplicate.data["existing_invoice_id"] == "INV-3002"
    assert duplicate.data["existing_status"] == "paid"


def test_file_invoice_reports_an_amount_mismatch_with_the_comparison(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    mismatch = tools.invoke(
        "erp.file_invoice",
        {
            "number": "CD-2026-007",
            "vendor_id": "V-1003",
            "amount": 2100.00,
            "po_id": "PO-2003",
            "gr_id": "GR-2503",
        },
    )

    assert mismatch.error_kind == "invalid"
    assert mismatch.data["mismatch"] == "purchase_order"
    assert mismatch.data["invoice_amount_cents"] == 210_000
    assert mismatch.data["expected_amount_cents"] == 200_000
    assert "2,100.00" in mismatch.summary
    assert "2,000.00" in mismatch.summary
    assert fetch(ledgerlite_db, "SELECT * FROM invoices WHERE number = 'CD-2026-007'") == []


def test_file_invoice_reports_a_goods_receipt_mismatch(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    mismatch = tools.invoke(
        "erp.file_invoice",
        {
            "number": "NW-2026-001",
            "vendor_id": "V-1001",
            "amount": 1250.00,
            "po_id": "PO-2001",
            "gr_id": "GR-2502",
        },
    )

    assert mismatch.error_kind == "invalid"
    assert mismatch.data["mismatch"] == "goods_receipt"
    assert mismatch.data["expected_amount_cents"] == 64_000


def test_schedule_payment_writes_a_scheduled_payment_for_the_full_invoice(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)
    filed = tools.invoke(
        "erp.file_invoice",
        {
            "number": "NW-2026-001",
            "vendor_id": "V-1001",
            "amount": 1250.00,
            "po_id": "PO-2001",
            "gr_id": "GR-2501",
        },
    )

    observation = tools.invoke(
        "erp.schedule_payment",
        {"invoice_id": filed.data["invoice_id"], "scheduled_for": "2026-10-05"},
    )

    assert observation.ok
    assert observation.data["amount_cents"] == 125_000
    assert observation.data["status"] == "scheduled"
    rows = fetch(
        ledgerlite_db,
        "SELECT * FROM payments WHERE invoice_id = ?",
        (filed.data["invoice_id"],),
    )
    assert len(rows) == 1
    assert rows[0]["id"] == observation.data["payment_id"]
    assert rows[0]["scheduled_for"] == "2026-10-05"


def test_schedule_payment_rejects_an_unknown_invoice_and_a_partial_amount(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    missing = tools.invoke("erp.schedule_payment", {"invoice_id": "INV-9999"})
    partial = tools.invoke(
        "erp.schedule_payment", {"invoice_id": "INV-3002", "amount": 100.00}
    )

    assert missing.error_kind == "not_found"
    assert partial.error_kind == "invalid"


def test_schedule_payment_policy_facts_read_the_vendor_status(ledgerlite_db: Path) -> None:
    tools = {tool.name: tool for tool in build_erp_tools(ledgerlite_db)}

    facts = tools["erp.schedule_payment"].policy_facts(
        {"invoice_id": "INV-3002", "amount": 640.00, "currency": "USD"}
    )

    assert facts == {
        "vendor_status": "active",
        "amount_usd": 640.0,
        "currency": "USD",
        "system": "ledgerlite",
    }


def test_create_vendor_writes_an_active_vendor_with_the_extracted_fields(
    ledgerlite_db: Path,
) -> None:
    tools = registry(ledgerlite_db)

    observation = tools.invoke(
        "erp.create_vendor",
        {
            "name": "Cascade Fabrication LLC",
            "tax_id": "TAX-2001",
            "email": "accounts@cascade-fabrication.example",
            "address": "4820 Foundry Way, Portland, OR 97210",
        },
    )

    assert observation.ok
    assert observation.data["vendor_id"] == "V-1009"
    assert observation.data["status"] == "active"
    rows = fetch(ledgerlite_db, "SELECT * FROM vendors WHERE id = 'V-1009'")
    assert len(rows) == 1
    assert rows[0]["name"] == "Cascade Fabrication LLC"
    assert rows[0]["tax_id"] == "TAX-2001"
    assert rows[0]["email"] == "accounts@cascade-fabrication.example"
    assert rows[0]["address"] == "4820 Foundry Way, Portland, OR 97210"
    assert rows[0]["scenario"] == "filed"
    assert fetch(ledgerlite_db, "SELECT * FROM vendors WHERE id = 'V-1010'") == []


def test_create_vendor_rejects_a_duplicate_tax_id_or_name(ledgerlite_db: Path) -> None:
    tools = registry(ledgerlite_db)

    by_tax_id = tools.invoke(
        "erp.create_vendor",
        {
            "name": "Apex Office Supply Co",
            "tax_id": "TAX-1002",
            "email": "billing@apex.example",
            "address": "1 Apex Way",
        },
    )
    by_name = tools.invoke(
        "erp.create_vendor",
        {
            "name": "Contoso Industrial",
            "tax_id": "TAX-9999",
            "email": "ap@contoso.example",
            "address": "1 Contoso Way",
        },
    )

    assert by_tax_id.error_kind == "invalid"
    assert by_tax_id.data["duplicate"] is True
    assert by_tax_id.data["existing_vendor_id"] == "V-1002"
    assert by_name.error_kind == "invalid"
    assert by_name.data["existing_vendor_id"] == "V-1000"
    assert fetch(ledgerlite_db, "SELECT * FROM vendors WHERE tax_id = 'TAX-9999'") == []


def test_create_vendor_policy_facts_name_the_system(ledgerlite_db: Path) -> None:
    tools = {tool.name: tool for tool in build_erp_tools(ledgerlite_db)}

    facts = tools["erp.create_vendor"].policy_facts(
        {"name": "Cascade Fabrication LLC", "tax_id": "TAX-2001", "email": "x@example.com"}
    )

    assert facts == {"system": "ledgerlite"}


def test_mutating_erp_tools_are_journaled_side_effects(ledgerlite_db: Path) -> None:
    tools = {tool.name: tool for tool in build_erp_tools(ledgerlite_db)}

    assert tools["erp.file_invoice"].side_effect
    assert tools["erp.schedule_payment"].side_effect
    assert tools["erp.create_vendor"].side_effect
    assert tools["erp.schedule_payment"].action == "payment.schedule"
    assert tools["erp.create_vendor"].action == "vendor.create"
    assert not tools["erp.get_invoice"].side_effect


def test_scheduling_a_payment_is_marked_irreversible(ledgerlite_db: Path) -> None:
    tools = {tool.name: tool for tool in build_erp_tools(ledgerlite_db)}

    assert tools["erp.schedule_payment"].irreversible
    assert tools["erp.create_vendor"].irreversible
    assert not tools["erp.file_invoice"].irreversible
    assert not tools["erp.get_invoice"].irreversible
