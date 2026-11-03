"""The Zoho Books accounting adapter: reads plus purchase-bill creation.

Zoho Books is the product system of record. The Verifier gets
``ZohoAccountingReads`` with a read-only token; the executor gets
``ZohoAccountingSystem`` with a separate write token. The adapter normalises
Zoho's vocabulary and money strings to the models the engine already uses, so a
Zoho Run and a Mock Suite Run behave the same way. The write records the Run's
idempotency key in the bill's reference number and reconciles before retrying.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from verirun.adapters.base import (
    AccountingReads,
    AccountingSystem,
    AdapterError,
    Approval,
    Bill,
    ConflictError,
    GoodsReceipt,
    NotFoundError,
    Payment,
    PurchaseOrder,
    Vendor,
    WriteOutcomeUnknownError,
)
from verirun.adapters.matching import gstin_key, name_key
from verirun.adapters.rules import duplicate_bill_error, require_positive_amount
from verirun.adapters.zoho.client import ZohoBooksClient

BILL_STATUSES = {
    "draft": "received",
    "pending_approval": "received",
    "open": "approved",
    "overdue": "approved",
    "partially_paid": "approved",
    "paid": "paid",
    "void": "rejected",
    "written_off": "rejected",
}
PURCHASE_ORDER_STATUSES = {
    "draft": "draft",
    "open": "open",
    "billed": "closed",
    "closed": "closed",
    "cancelled": "cancelled",
    "void": "cancelled",
}


class ZohoAccountingReads(AccountingReads):
    """Read vendors, purchase orders, bills, and payments from Zoho Books."""

    display_name = "Zoho Books"
    policy_system = "zoho_books"

    def __init__(
        self,
        client: ZohoBooksClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self._sleep = sleep
        self._clock = clock

    # Vendors ---------------------------------------------------------------

    def list_vendors(self, *, status: str | None = None) -> list[Vendor]:
        vendors = [
            _vendor(item)
            for item in self.client.get_all("/contacts", "contacts", {"contact_type": "vendor"})
            if _is_vendor(item)
        ]
        if status is not None:
            vendors = [item for item in vendors if item.status == status.casefold()]
        return sorted(vendors, key=lambda item: item.id)

    def get_vendor(self, vendor_id: str) -> Vendor:
        payload = self.client.get(f"/contacts/{vendor_id}")
        contact = payload.get("contact")
        if not isinstance(contact, dict) or not _is_vendor(contact):
            raise NotFoundError(f"no vendor '{vendor_id}' in Zoho Books")
        return _vendor(contact)

    def find_vendors(
        self, *, name: str | None = None, tax_id: str | None = None
    ) -> list[Vendor]:
        vendors = self.list_vendors()
        if name is None and tax_id is None:
            return vendors
        wanted_tax_id = gstin_key(tax_id) if tax_id is not None else None
        wanted_name = name_key(name) if name is not None else None
        return [
            vendor
            for vendor in vendors
            if (wanted_tax_id is not None and gstin_key(vendor.tax_id) == wanted_tax_id)
            or (wanted_name is not None and name_key(vendor.name) == wanted_name)
        ]

    # Purchase orders and receipts ------------------------------------------

    def list_purchase_orders(
        self, *, vendor_id: str | None = None, status: str | None = None
    ) -> list[PurchaseOrder]:
        params = {"vendor_id": vendor_id} if vendor_id is not None else None
        orders = [
            _purchase_order(item)
            for item in self.client.get_all("/purchaseorders", "purchaseorders", params)
        ]
        if status is not None:
            orders = [item for item in orders if item.status == status.casefold()]
        return sorted(orders, key=lambda item: item.id)

    def get_purchase_order(self, po_id: str) -> PurchaseOrder:
        payload = self.client.get(f"/purchaseorders/{po_id}")
        order = payload.get("purchaseorder")
        if not isinstance(order, dict):
            raise NotFoundError(f"no purchase order '{po_id}' in Zoho Books")
        return _purchase_order(order)

    def get_goods_receipt(self, gr_id: str) -> GoodsReceipt:
        raise NotFoundError(
            f"Zoho Books records no goods receipts; cannot fetch '{gr_id}'"
        )

    # Bills and payments -----------------------------------------------------

    def list_bills(
        self,
        *,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
    ) -> list[Bill]:
        params = {"vendor_id": vendor_id} if vendor_id is not None else None
        bills = [
            _bill(item)
            for item in self.client.get_all("/bills", "bills", params)
        ]
        if number is not None:
            wanted = number.strip().casefold()
            bills = [item for item in bills if item.number.strip().casefold() == wanted]
        if vendor_id is not None:
            bills = [item for item in bills if item.vendor_id == vendor_id]
        if status is not None:
            bills = [item for item in bills if item.status == status.casefold()]
        return sorted(bills, key=lambda item: item.id)

    def get_bill(self, bill_id: str) -> Bill:
        payload = self.client.get(f"/bills/{bill_id}")
        bill = payload.get("bill")
        if not isinstance(bill, dict):
            raise NotFoundError(f"no bill '{bill_id}' in Zoho Books")
        return _bill(bill)

    def wait_for_bill(
        self,
        *,
        number: str,
        vendor_id: str,
        timeout_s: float = 30.0,
        interval_s: float = 1.0,
    ) -> Bill:
        """Poll until Zoho Books returns the bill a write has just created.

        Zoho list endpoints can lag a successful create. A timeout is a
        ``NotFoundError`` for the writer to reconcile, never a reason to retry
        the write blindly. More than one bill is a conflict.
        """
        deadline = self._clock() + timeout_s
        while True:
            bills = self.list_bills(number=number, vendor_id=vendor_id)
            if len(bills) == 1:
                return bills[0]
            if len(bills) > 1:
                raise ConflictError(
                    f"Zoho Books returned {len(bills)} bills numbered {number} for "
                    f"vendor {vendor_id}, expected one"
                )
            if self._clock() >= deadline:
                raise NotFoundError(
                    f"Zoho Books has not returned bill {number} for vendor "
                    f"{vendor_id} after {timeout_s:g}s"
                )
            self._sleep(interval_s)

    def list_payments(self, bill_id: str) -> list[Payment]:
        payments = [
            _payment(item, bill_id)
            for item in self.client.get_all(
                "/vendorpayments", "vendorpayments", {"bill_id": bill_id}
            )
        ]
        return sorted(payments, key=lambda item: item.id)

    def find_pending_approval(self, bill_id: str) -> Approval | None:
        """Zoho Books has no pending-approval record on a bill."""
        return None


class ZohoAccountingSystem(ZohoAccountingReads, AccountingSystem):
    """The executor's Zoho Books handle: reads plus purchase-bill creation.

    Zoho Books has no idempotency header, so the adapter records the Run's key
    in the bill's reference number and reconciles by (vendor, invoice number)
    before it repeats a write. A bill that exists without this Run's key is a
    duplicate, never adopted silently. An unknown write outcome is reconciled
    once and then raised for a human; it is never retried blindly.
    """

    def __init__(
        self,
        reads: ZohoAccountingReads,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            reads.client,
            sleep=sleep or reads._sleep,
            clock=clock or reads._clock,
        )

    def create_purchase_bill(
        self,
        *,
        number: str,
        vendor_id: str,
        amount_cents: int,
        currency: str = "USD",
        po_id: str | None = None,
        gr_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Bill:
        require_positive_amount(amount_cents)
        if gr_id:
            raise ConflictError(
                "Zoho Books has no goods receipts; a bill cannot reference one"
            )
        self.get_vendor(vendor_id)
        marker = _reference_marker(idempotency_key)
        duplicate = self._existing_bill(number=number, vendor_id=vendor_id)
        if duplicate is not None:
            if marker and duplicate.reference == marker:
                return duplicate
            raise duplicate_bill_error(
                number=number,
                vendor_id=vendor_id,
                existing_id=duplicate.id,
                existing_status=duplicate.status,
            )
        body: dict[str, Any] = {
            "vendor_id": vendor_id,
            "bill_number": number,
            "date": datetime.now(UTC).date().isoformat(),
            "currency_code": currency,
            "line_items": [
                {
                    "name": f"Invoice {number}",
                    "rate": amount_cents / 100,
                    "quantity": 1,
                }
            ],
        }
        if po_id:
            body["purchaseorder_ids"] = [po_id]
        if marker:
            body["reference_number"] = marker
        try:
            payload = self.client.post("/bills", body)
        except WriteOutcomeUnknownError:
            # A read that fails during reconciliation must not turn an unknown
            # write into a retry: escalate the original outcome instead.
            try:
                reconciled = self._reconcile(
                    number=number, vendor_id=vendor_id, marker=marker
                )
            except AdapterError:
                reconciled = None
            if reconciled is not None:
                return reconciled
            raise
        bill = payload.get("bill")
        if not isinstance(bill, dict):
            raise ConflictError("Zoho Books returned no bill after the write")
        created = _bill(bill)
        try:
            return self.wait_for_bill(number=number, vendor_id=vendor_id)
        except NotFoundError:
            # The create response is the authoritative record; a list endpoint
            # that has not caught up must not turn a completed write into a
            # retry.
            return created

    def _existing_bill(self, *, number: str, vendor_id: str) -> Bill | None:
        bills = self.list_bills(number=number, vendor_id=vendor_id)
        if len(bills) > 1:
            raise ConflictError(
                f"Zoho Books returned {len(bills)} bills numbered {number} for "
                f"vendor {vendor_id}, expected at most one"
            )
        return bills[0] if bills else None

    def _reconcile(
        self, *, number: str, vendor_id: str, marker: str
    ) -> Bill | None:
        bill = self._existing_bill(number=number, vendor_id=vendor_id)
        if bill is not None and marker and bill.reference == marker:
            return bill
        return None


def _reference_marker(idempotency_key: str | None) -> str:
    """The value Verirun writes into a Zoho bill's reference number."""
    return f"verirun:{idempotency_key}" if idempotency_key else ""


def _is_vendor(contact: dict[str, Any]) -> bool:
    return str(contact.get("contact_type") or "vendor").casefold() == "vendor"


def _vendor(contact: dict[str, Any]) -> Vendor:
    return Vendor(
        id=_required_text(
            contact.get("contact_id") or contact.get("id"), "vendor id"
        ),
        name=_required_text(
            contact.get("contact_name") or contact.get("company_name"),
            "vendor name",
        ),
        tax_id=_text(contact.get("gst_no") or contact.get("tax_id")),
        email=_text(contact.get("email")),
        address=_address(contact.get("billing_address")),
        status=_text(contact.get("status") or "active").casefold(),
        created_at=_text(contact.get("created_time") or contact.get("created_at")),
    )


def _purchase_order(order: dict[str, Any]) -> PurchaseOrder:
    po_id = _required_text(
        order.get("purchaseorder_id")
        or order.get("purchase_order_id")
        or order.get("id"),
        "purchase order id",
    )
    return PurchaseOrder(
        id=po_id,
        vendor_id=_required_text(
            order.get("vendor_id"), "purchase order vendor"
        ),
        amount_cents=_cents(order.get("total"), "purchase order total"),
        currency=_text(order.get("currency_code") or "INR"),
        status=PURCHASE_ORDER_STATUSES.get(
            _text(order.get("status")).casefold(), _text(order.get("status")).casefold()
        ),
        issued_at=_text(order.get("date") or order.get("created_time")),
        number=_text(order.get("purchaseorder_number") or po_id),
    )


def _bill(bill: dict[str, Any]) -> Bill:
    po_ids = bill.get("purchaseorder_ids") or bill.get("purchase_order_ids") or []
    if isinstance(po_ids, dict):
        po_ids = [po_ids]
    po_id = None
    if isinstance(po_ids, list) and po_ids:
        first = po_ids[0]
        po_id = str(first.get("purchaseorder_id") if isinstance(first, dict) else first)
    raw_status = _text(bill.get("status") or "draft").casefold().replace(" ", "_")
    return Bill(
        id=_required_text(bill.get("bill_id") or bill.get("id"), "bill id"),
        number=_required_text(
            bill.get("bill_number") or bill.get("number"), "bill number"
        ),
        vendor_id=_required_text(bill.get("vendor_id"), "bill vendor"),
        po_id=po_id,
        gr_id=None,
        amount_cents=_cents(bill.get("total"), "bill total"),
        currency=_text(bill.get("currency_code") or "INR"),
        status=BILL_STATUSES.get(raw_status, raw_status),
        received_at=_text(bill.get("date") or bill.get("created_time")),
        reference=_text(bill.get("reference_number")),
    )


def _payment(payment: dict[str, Any], bill_id: str) -> Payment:
    return Payment(
        id=_required_text(
            payment.get("payment_id") or payment.get("id"), "payment id"
        ),
        bill_id=_text(payment.get("bill_id") or bill_id),
        amount_cents=_cents(payment.get("amount"), "payment amount"),
        currency=_text(payment.get("currency_code") or "INR"),
        scheduled_for=_text(payment.get("date") or payment.get("scheduled_for")),
        status=_text(payment.get("status") or "paid").casefold(),
    )


def _address(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ""
    parts = [
        raw.get("address"),
        raw.get("city"),
        raw.get("state"),
        raw.get("zip"),
        raw.get("country"),
    ]
    return ", ".join(str(part) for part in parts if part)


def _cents(raw: Any, field: str) -> int:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdapterError(f"Zoho Books returned an invalid {field}: {raw!r}") from exc
    paise = value * 100
    if paise != paise.to_integral_value():
        raise AdapterError(
            f"Zoho Books {field} has more than two decimals: {raw!r}"
        )
    return int(paise)


def _text(raw: Any) -> str:
    return "" if raw is None else str(raw)


def _required_text(raw: Any, field: str) -> str:
    text = _text(raw)
    if not text:
        raise AdapterError(f"Zoho Books returned no {field}")
    return text
