"""ERP tools: read and write the accounting system through its adapter.

The engine depends on ``AccountingSystem``; the Mock Suite's LedgerLite adapter
implements it for the demo, eval, and tests, and the in-memory fake implements
it for deterministic engine tests.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from verirun.adapters import AccountingSystem, AdapterError
from verirun.engine.models import Observation
from verirun.tools.base import Tool, ToolError, optional_str, require_str


def parse_amount(raw: Any) -> int:
    """Parse a money amount into whole cents, rejecting anything ambiguous."""
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


class ErpTool(Tool):
    """An ERP tool bound to one accounting system."""

    def __init__(self, accounting: AccountingSystem) -> None:
        self.accounting = accounting


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
        vendors = self.adapter_call(self.accounting.list_vendors, status=optional_str(args, "status"))
        return Observation(
            ok=True,
            summary=f"Listed {len(vendors)} vendor(s)",
            data={"vendors": [vendor.as_dict() for vendor in vendors]},
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
        vendor = self.adapter_call(self.accounting.get_vendor, vendor_id)
        return Observation(
            ok=True,
            summary=f"Fetched vendor {vendor_id}",
            data={"vendor": vendor.as_dict()},
        )


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
        orders = self.adapter_call(
            self.accounting.list_purchase_orders,
            vendor_id=optional_str(args, "vendor_id"),
            status=optional_str(args, "status"),
        )
        return Observation(
            ok=True,
            summary=f"Listed {len(orders)} purchase order(s)",
            data={"purchase_orders": [order.as_dict() for order in orders]},
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
        order = self.adapter_call(self.accounting.get_purchase_order, po_id)
        return Observation(
            ok=True,
            summary=f"Fetched purchase order {po_id}",
            data={"purchase_order": order.as_dict()},
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
        receipt = self.adapter_call(self.accounting.get_goods_receipt, gr_id)
        return Observation(
            ok=True,
            summary=f"Fetched goods receipt {gr_id}",
            data={"goods_receipt": receipt.as_dict()},
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
        bills = self.adapter_call(
            self.accounting.list_bills,
            number=optional_str(args, "number"),
            vendor_id=optional_str(args, "vendor_id"),
            status=optional_str(args, "status"),
        )
        return Observation(
            ok=True,
            summary=f"Listed {len(bills)} invoice(s)",
            data={"invoices": [bill.as_dict() for bill in bills]},
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
        bill = self.adapter_call(self.accounting.get_bill, invoice_id)
        return Observation(
            ok=True,
            summary=f"Fetched invoice {invoice_id}",
            data={"invoice": bill.as_dict()},
        )


class CreateVendorTool(ErpTool):
    name = "erp.create_vendor"
    description = (
        "Create a vendor in LedgerLite from a validated tax form. A vendor with "
        "the same tax id or name is rejected; tax ids are never invented."
    )
    side_effect = True
    irreversible = True
    action = "vendor.create"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The vendor's legal name."},
            "tax_id": {"type": "string", "description": "The tax id from the tax form."},
            "email": {"type": "string", "description": "The vendor's accounts contact email."},
            "address": {"type": "string", "description": "The vendor's address from the tax form."},
        },
        "required": ["name", "tax_id", "email", "address"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any]) -> Observation:
        name = require_str(args, "name")
        tax_id = require_str(args, "tax_id")
        email = require_str(args, "email")
        address = require_str(args, "address")
        vendor = self.adapter_call(
            self.accounting.create_vendor,
            name=name,
            tax_id=tax_id,
            email=email,
            address=address,
        )
        return Observation(
            ok=True,
            summary=f"Created vendor {vendor.id} ({vendor.name}) with tax id {vendor.tax_id}",
            data={
                "vendor_id": vendor.id,
                "name": vendor.name,
                "tax_id": vendor.tax_id,
                "email": vendor.email,
                "address": vendor.address,
                "status": vendor.status,
            },
        )

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"system": self.accounting.policy_system}


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
        bill = self.adapter_call(
            self.accounting.create_purchase_bill,
            number=number,
            vendor_id=vendor_id,
            amount_cents=amount_cents,
            currency=currency,
            po_id=po_id,
            gr_id=gr_id,
        )
        return Observation(
            ok=True,
            summary=(
                f"Filed invoice {bill.number} as {bill.id} for "
                f"{bill.amount_cents / 100:,.2f} {bill.currency}"
            ),
            data={
                "invoice_id": bill.id,
                "number": bill.number,
                "vendor_id": bill.vendor_id,
                "amount_cents": bill.amount_cents,
                "currency": bill.currency,
                "po_id": bill.po_id,
                "gr_id": bill.gr_id,
                "status": bill.status,
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
        raw_amount = args.get("amount")
        amount_cents = parse_amount(raw_amount) if raw_amount is not None else None
        payment = self.adapter_call(
            self.accounting.schedule_payment,
            bill_id=invoice_id,
            amount_cents=amount_cents,
            scheduled_for=optional_str(args, "scheduled_for"),
        )
        return Observation(
            ok=True,
            summary=(
                f"Scheduled payment {payment.id} of "
                f"{payment.amount_cents / 100:,.2f} {payment.currency} "
                f"for {invoice_id} on {payment.scheduled_for}"
            ),
            data={
                "payment_id": payment.id,
                "invoice_id": payment.bill_id,
                "amount_cents": payment.amount_cents,
                "currency": payment.currency,
                "scheduled_for": payment.scheduled_for,
                "status": payment.status,
            },
        )

    def policy_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args.get("invoice_id")
        if not isinstance(invoice_id, str) or not invoice_id:
            return {
                "vendor_status": "unknown",
                "amount_usd": 0,
                "currency": "USD",
                "system": self.accounting.policy_system,
            }
        try:
            bill = self.accounting.get_bill(invoice_id)
        except AdapterError:
            return {
                "vendor_status": "unknown",
                "amount_usd": 0,
                "currency": "USD",
                "system": self.accounting.policy_system,
            }
        try:
            vendor = self.accounting.get_vendor(bill.vendor_id)
        except AdapterError:
            vendor = None
        amount_cents = bill.amount_cents
        raw_amount = args.get("amount")
        if raw_amount is not None:
            try:
                amount_cents = parse_amount(raw_amount)
            except ToolError:
                pass
        return {
            "vendor_status": vendor.status if vendor else "unknown",
            "amount_usd": amount_cents / 100,
            "currency": bill.currency,
            "system": self.accounting.policy_system,
        }


def build_erp_tools(accounting: AccountingSystem) -> list[Tool]:
    """The ERP tools, all bound to one accounting system."""
    return [
        ListVendorsTool(accounting),
        GetVendorTool(accounting),
        ListPurchaseOrdersTool(accounting),
        GetPurchaseOrderTool(accounting),
        GetGoodsReceiptTool(accounting),
        ListInvoicesTool(accounting),
        GetInvoiceTool(accounting),
        CreateVendorTool(accounting),
        FileInvoiceTool(accounting),
        SchedulePaymentTool(accounting),
    ]
