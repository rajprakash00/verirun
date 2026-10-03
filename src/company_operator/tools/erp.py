"""ERP tools: read and write LedgerLite, the internal system of record.

Reads answer from the same SQLite database the LedgerLite app serves. The two
write tools mirror the UI's behaviour: ``erp.file_invoice`` consumes a seeded
transient-failure flag before inserting, and ``erp.schedule_payment`` reports
the vendor status and amount the policy gate needs before it runs.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

from company_operator.engine.models import Observation
from company_operator.tools.base import Tool, ToolError, optional_str, require_str

READ_ACTIONS = (
    "erp.list_vendors",
    "erp.get_vendor",
    "erp.list_purchase_orders",
    "erp.get_purchase_order",
    "erp.get_goods_receipt",
    "erp.list_invoices",
    "erp.get_invoice",
)


def parse_amount(raw: Any) -> int:
    """Parse a dollar amount into whole cents, rejecting anything ambiguous."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ToolError("invalid", f"amount must be a number, got {raw!r}")
    text = str(raw).strip().lstrip("$").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ToolError("invalid", f"cannot parse amount {raw!r}") from exc
    cents = value * 100
    if cents <= 0 or cents != cents.to_integral_value():
        raise ToolError("invalid", f"amount must be a positive whole number of cents, got {raw!r}")
    return int(cents)


def _money(cents: int, currency: str) -> str:
    return f"{cents / 100:,.2f} {currency}"


def _next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE id LIKE ?"
        " ORDER BY CAST(SUBSTR(id, ?) AS INTEGER) DESC LIMIT 1",
        (f"{prefix}-%", len(prefix) + 2),
    ).fetchone()
    number = int(row["id"].split("-")[1]) + 1 if row else 1001
    return f"{prefix}-{number}"


def _consume_failure(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT remaining, message FROM failure_flags WHERE key = ?", (key,)
    ).fetchone()
    if row is None or row["remaining"] <= 0:
        return None
    conn.execute("UPDATE failure_flags SET remaining = remaining - 1 WHERE key = ?", (key,))
    conn.commit()
    return row["message"] or "LedgerLite is temporarily unavailable; retry the request."


class ErpTool(Tool):
    """An ERP tool bound to one LedgerLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise ToolError("not_found", f"LedgerLite database not found: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def one(self, conn: sqlite3.Connection, table: str, row_id: str, kind: str) -> dict[str, Any]:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise ToolError("not_found", f"no {kind} '{row_id}' in LedgerLite")
        return dict(row)

    def many(
        self, conn: sqlite3.Connection, table: str, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [dict(row) for row in conn.execute(sql, params)]


class ListVendorsTool(ErpTool):
    name = "erp.list_vendors"
    description = "List vendors in LedgerLite, optionally filtered by status."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status: active, blocked, pending."}
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        with closing(self.connect()) as conn:
            vendors = self.many(conn, "vendors", {"status": optional_str(args, "status")})
        return Observation(
            ok=True,
            summary=f"Listed {len(vendors)} vendor(s)",
            data={"vendors": vendors},
        )


class GetVendorTool(ErpTool):
    name = "erp.get_vendor"
    description = "Fetch one vendor by id."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"vendor_id": {"type": "string"}},
        "required": ["vendor_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        vendor_id = require_str(args, "vendor_id")
        with closing(self.connect()) as conn:
            vendor = self.one(conn, "vendors", vendor_id, "vendor")
        return Observation(ok=True, summary=f"Fetched vendor {vendor_id}", data={"vendor": vendor})


class ListPurchaseOrdersTool(ErpTool):
    name = "erp.list_purchase_orders"
    description = "List purchase orders in LedgerLite, optionally filtered by vendor or status."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "vendor_id": {"type": "string"},
            "status": {"type": "string", "description": "open, closed, or cancelled."},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        with closing(self.connect()) as conn:
            orders = self.many(
                conn,
                "purchase_orders",
                {"vendor_id": optional_str(args, "vendor_id"), "status": optional_str(args, "status")},
            )
        return Observation(
            ok=True,
            summary=f"Listed {len(orders)} purchase order(s)",
            data={"purchase_orders": orders},
        )


class GetPurchaseOrderTool(ErpTool):
    name = "erp.get_purchase_order"
    description = "Fetch one purchase order by id."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"po_id": {"type": "string"}},
        "required": ["po_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        po_id = require_str(args, "po_id")
        with closing(self.connect()) as conn:
            order = self.one(conn, "purchase_orders", po_id, "purchase order")
        return Observation(
            ok=True, summary=f"Fetched purchase order {po_id}", data={"purchase_order": order}
        )


class GetGoodsReceiptTool(ErpTool):
    name = "erp.get_goods_receipt"
    description = "Fetch one goods receipt by id."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"gr_id": {"type": "string"}},
        "required": ["gr_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        gr_id = require_str(args, "gr_id")
        with closing(self.connect()) as conn:
            receipt = self.one(conn, "goods_receipts", gr_id, "goods receipt")
        return Observation(
            ok=True, summary=f"Fetched goods receipt {gr_id}", data={"goods_receipt": receipt}
        )


class ListInvoicesTool(ErpTool):
    name = "erp.list_invoices"
    description = "List invoices in LedgerLite, optionally filtered by number, vendor, or status."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Vendor invoice number."},
            "vendor_id": {"type": "string"},
            "status": {"type": "string", "description": "received, approved, paid, or rejected."},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        with closing(self.connect()) as conn:
            invoices = self.many(
                conn,
                "invoices",
                {
                    "number": optional_str(args, "number"),
                    "vendor_id": optional_str(args, "vendor_id"),
                    "status": optional_str(args, "status"),
                },
            )
        return Observation(
            ok=True,
            summary=f"Listed {len(invoices)} invoice(s)",
            data={"invoices": invoices},
        )


class GetInvoiceTool(ErpTool):
    name = "erp.get_invoice"
    description = "Fetch one invoice by id."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ["invoice_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        invoice_id = require_str(args, "invoice_id")
        with closing(self.connect()) as conn:
            invoice = self.one(conn, "invoices", invoice_id, "invoice")
        return Observation(
            ok=True, summary=f"Fetched invoice {invoice_id}", data={"invoice": invoice}
        )


class FileInvoiceTool(ErpTool):
    name = "erp.file_invoice"
    description = (
        "File a validated invoice in LedgerLite against its vendor, purchase order, "
        "and goods receipt. Amount is in US dollars."
    )
    side_effect = True
    action = "invoice.file"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Vendor invoice number."},
            "vendor_id": {"type": "string"},
            "amount": {
                "type": "number",
                "description": "Invoice amount in US dollars, for example 1250.00.",
            },
            "po_id": {"type": "string", "description": "Purchase order id, when the invoice cites one."},
            "gr_id": {"type": "string", "description": "Goods receipt id, when the invoice cites one."},
            "currency": {"type": "string", "description": "Currency code. Defaults to USD."},
        },
        "required": ["number", "vendor_id", "amount"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        number = require_str(args, "number")
        vendor_id = require_str(args, "vendor_id")
        amount_cents = parse_amount(args.get("amount"))
        po_id = optional_str(args, "po_id")
        gr_id = optional_str(args, "gr_id")
        currency = optional_str(args, "currency", "USD") or "USD"
        with closing(self.connect()) as conn:
            self.one(conn, "vendors", vendor_id, "vendor")
            existing = conn.execute(
                "SELECT id, status FROM invoices WHERE vendor_id = ? AND number = ?",
                (vendor_id, number),
            ).fetchone()
            if existing is not None:
                raise ToolError(
                    "invalid",
                    f"Invoice {number} already exists for {vendor_id} as {existing['id']}",
                    data={
                        "duplicate": True,
                        "number": number,
                        "vendor_id": vendor_id,
                        "existing_invoice_id": existing["id"],
                        "existing_status": existing["status"],
                    },
                )
            if po_id:
                order = self.one(conn, "purchase_orders", po_id, "purchase order")
                if order["amount_cents"] != amount_cents:
                    raise ToolError(
                        "invalid",
                        (
                            f"Invoice {number} is {_money(amount_cents, currency)} but "
                            f"purchase order {po_id} is "
                            f"{_money(order['amount_cents'], currency)}"
                        ),
                        data={
                            "mismatch": "purchase_order",
                            "number": number,
                            "vendor_id": vendor_id,
                            "po_id": po_id,
                            "invoice_amount_cents": amount_cents,
                            "expected_amount_cents": order["amount_cents"],
                            "currency": currency,
                        },
                    )
            if gr_id:
                receipt = self.one(conn, "goods_receipts", gr_id, "goods receipt")
                if receipt["amount_cents"] != amount_cents:
                    raise ToolError(
                        "invalid",
                        (
                            f"Invoice {number} is {_money(amount_cents, currency)} but "
                            f"goods receipt {gr_id} is "
                            f"{_money(receipt['amount_cents'], currency)}"
                        ),
                        data={
                            "mismatch": "goods_receipt",
                            "number": number,
                            "vendor_id": vendor_id,
                            "gr_id": gr_id,
                            "invoice_amount_cents": amount_cents,
                            "expected_amount_cents": receipt["amount_cents"],
                            "currency": currency,
                        },
                    )
            failure = _consume_failure(conn, f"invoice.create:{number}")
            if failure is not None:
                raise ToolError("transient", failure)
            invoice_id = _next_id(conn, "invoices", "INV")
            conn.execute(
                """
                INSERT INTO invoices
                    (id, number, vendor_id, po_id, gr_id, amount_cents, currency, status,
                     received_at, scenario)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, 'filed')
                """,
                (
                    invoice_id,
                    number,
                    vendor_id,
                    po_id,
                    gr_id,
                    amount_cents,
                    currency,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        return Observation(
            ok=True,
            summary=f"Filed invoice {number} as {invoice_id} for {amount_cents / 100:,.2f} {currency}",
            data={
                "invoice_id": invoice_id,
                "number": number,
                "vendor_id": vendor_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "po_id": po_id,
                "gr_id": gr_id,
                "status": "received",
            },
        )


class SchedulePaymentTool(ErpTool):
    name = "erp.schedule_payment"
    description = (
        "Schedule the full payment for a filed invoice in LedgerLite. "
        "Partial or split payments are rejected."
    )
    side_effect = True
    irreversible = True
    action = "payment.schedule"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "invoice_id": {"type": "string", "description": "LedgerLite invoice id."},
            "amount": {
                "type": "number",
                "description": "Optional amount in US dollars; must equal the invoice total.",
            },
            "scheduled_for": {
                "type": "string",
                "description": "Payment date as YYYY-MM-DD. Defaults to 30 days from today.",
            },
        },
        "required": ["invoice_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        invoice_id = require_str(args, "invoice_id")
        scheduled_for = optional_str(args, "scheduled_for")
        with closing(self.connect()) as conn:
            invoice = self.one(conn, "invoices", invoice_id, "invoice")
            amount_cents = self._full_amount(invoice, args.get("amount"))
            payment_id = _next_id(conn, "payments", "PAY")
            due = scheduled_for or (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
            conn.execute(
                """
                INSERT INTO payments
                    (id, invoice_id, amount_cents, currency, scheduled_for, status, scenario)
                VALUES (?, ?, ?, ?, ?, 'scheduled', 'filed')
                """,
                (payment_id, invoice_id, amount_cents, invoice["currency"], due),
            )
            conn.commit()
        return Observation(
            ok=True,
            summary=(
                f"Scheduled payment {payment_id} of {amount_cents / 100:,.2f} "
                f"{invoice['currency']} for {invoice_id} on {due}"
            ),
            data={
                "payment_id": payment_id,
                "invoice_id": invoice_id,
                "amount_cents": amount_cents,
                "currency": invoice["currency"],
                "scheduled_for": due,
                "status": "scheduled",
            },
        )

    def _full_amount(self, invoice: dict[str, Any], raw: Any) -> int:
        if raw is None:
            return int(invoice["amount_cents"])
        amount_cents = parse_amount(raw)
        if amount_cents != invoice["amount_cents"]:
            raise ToolError(
                "invalid",
                f"payment must cover the full invoice {invoice['id']} "
                f"({invoice['amount_cents'] / 100:,.2f} {invoice['currency']}); "
                f"splitting is forbidden",
            )
        return amount_cents

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args.get("invoice_id")
        if not isinstance(invoice_id, str) or not invoice_id:
            return {"vendor_status": "unknown", "amount_usd": 0, "currency": "USD", "system": "ledgerlite"}
        with closing(self.connect()) as conn:
            invoice = conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
            if invoice is None:
                return {
                    "vendor_status": "unknown",
                    "amount_usd": 0,
                    "currency": "USD",
                    "system": "ledgerlite",
                }
            vendor = conn.execute(
                "SELECT status FROM vendors WHERE id = ?", (invoice["vendor_id"],)
            ).fetchone()
        amount_cents = invoice["amount_cents"]
        raw_amount = args.get("amount")
        if raw_amount is not None:
            try:
                amount_cents = parse_amount(raw_amount)
            except ToolError:
                pass
        return {
            "vendor_status": vendor["status"] if vendor else "unknown",
            "amount_usd": amount_cents / 100,
            "currency": invoice["currency"],
            "system": "ledgerlite",
        }


def build_erp_tools(db_path: str | Path) -> list[Tool]:
    """The ERP tools, all bound to one LedgerLite database."""
    return [
        ListVendorsTool(db_path),
        GetVendorTool(db_path),
        ListPurchaseOrdersTool(db_path),
        GetPurchaseOrderTool(db_path),
        GetGoodsReceiptTool(db_path),
        ListInvoicesTool(db_path),
        GetInvoiceTool(db_path),
        FileInvoiceTool(db_path),
        SchedulePaymentTool(db_path),
    ]
