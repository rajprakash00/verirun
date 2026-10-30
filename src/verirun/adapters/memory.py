"""An in-memory accounting system for deterministic engine and crash tests.

No database, no network, no Mock Suite. The fake enforces the same business
rules as the Mock Suite adapter: duplicate bills are rejected, bill amounts must
agree with their purchase order and goods receipt, and a payment covers the
whole bill.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from verirun.adapters.base import (
    AccountingSystem,
    Approval,
    Bill,
    GoodsReceipt,
    NotFoundError,
    Payment,
    PurchaseOrder,
    Vendor,
)
from verirun.adapters.rules import (
    bill_reference_mismatch,
    duplicate_bill_error,
    duplicate_vendor_error,
    partial_payment_error,
    require_positive_amount,
)


class InMemoryAccountingSystem(AccountingSystem):
    """A complete accounting system held in dictionaries."""

    display_name = "the in-memory accounting system"
    # Stands in for LedgerLite in the Mock Suite Task Packs, so the shipped
    # policies apply unchanged to deterministic engine tests.
    policy_system = "ledgerlite"

    def __init__(
        self,
        *,
        vendors: Iterable[Vendor] = (),
        purchase_orders: Iterable[PurchaseOrder] = (),
        goods_receipts: Iterable[GoodsReceipt] = (),
        bills: Iterable[Bill] = (),
        payments: Iterable[Payment] = (),
        approvals: Iterable[Approval] = (),
    ) -> None:
        self._vendors = {item.id: item for item in vendors}
        self._purchase_orders = {item.id: item for item in purchase_orders}
        self._goods_receipts = {item.id: item for item in goods_receipts}
        self._bills = {item.id: item for item in bills}
        self._payments = {item.id: item for item in payments}
        self._approvals = {item.id: item for item in approvals}

    # Reads -----------------------------------------------------------------

    def list_vendors(self, *, status: str | None = None) -> list[Vendor]:
        return [
            vendor
            for vendor in self._sorted(self._vendors)
            if status is None or vendor.status == status
        ]

    def get_vendor(self, vendor_id: str) -> Vendor:
        vendor = self._vendors.get(vendor_id)
        if vendor is None:
            raise NotFoundError(f"no vendor '{vendor_id}' in the accounting system")
        return vendor

    def find_vendors(
        self, *, name: str | None = None, tax_id: str | None = None
    ) -> list[Vendor]:
        return [
            vendor
            for vendor in self._sorted(self._vendors)
            if (name is None and tax_id is None)
            or (tax_id is not None and vendor.tax_id == tax_id)
            or (name is not None and vendor.name == name)
        ]

    def list_purchase_orders(
        self, *, vendor_id: str | None = None, status: str | None = None
    ) -> list[PurchaseOrder]:
        return [
            order
            for order in self._sorted(self._purchase_orders)
            if (vendor_id is None or order.vendor_id == vendor_id)
            and (status is None or order.status == status)
        ]

    def get_purchase_order(self, po_id: str) -> PurchaseOrder:
        order = self._purchase_orders.get(po_id)
        if order is None:
            raise NotFoundError(f"no purchase order '{po_id}' in the accounting system")
        return order

    def get_goods_receipt(self, gr_id: str) -> GoodsReceipt:
        receipt = self._goods_receipts.get(gr_id)
        if receipt is None:
            raise NotFoundError(f"no goods receipt '{gr_id}' in the accounting system")
        return receipt

    def list_bills(
        self,
        *,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
    ) -> list[Bill]:
        return [
            bill
            for bill in self._sorted(self._bills)
            if (number is None or bill.number == number)
            and (vendor_id is None or bill.vendor_id == vendor_id)
            and (status is None or bill.status == status)
        ]

    def get_bill(self, bill_id: str) -> Bill:
        bill = self._bills.get(bill_id)
        if bill is None:
            raise NotFoundError(f"no invoice '{bill_id}' in the accounting system")
        return bill

    def list_payments(self, bill_id: str) -> list[Payment]:
        return [
            payment
            for payment in self._sorted(self._payments)
            if payment.bill_id == bill_id
        ]

    def find_pending_approval(self, bill_id: str) -> Approval | None:
        for approval in self._sorted(self._approvals):
            if (
                approval.subject_type == "invoice"
                and approval.subject_id == bill_id
                and approval.status == "pending"
            ):
                return approval
        return None

    # Writes ----------------------------------------------------------------

    def create_purchase_bill(
        self,
        *,
        number: str,
        vendor_id: str,
        amount_cents: int,
        currency: str = "USD",
        po_id: str | None = None,
        gr_id: str | None = None,
    ) -> Bill:
        require_positive_amount(amount_cents)
        po_id = po_id or None
        gr_id = gr_id or None
        if vendor_id not in self._vendors:
            raise NotFoundError(f"no vendor '{vendor_id}' in the accounting system")
        duplicate = next(
            (
                bill
                for bill in self._bills.values()
                if bill.vendor_id == vendor_id and bill.number == number
            ),
            None,
        )
        if duplicate is not None:
            raise duplicate_bill_error(
                number=number,
                vendor_id=vendor_id,
                existing_id=duplicate.id,
                existing_status=duplicate.status,
            )
        if po_id:
            order = self.get_purchase_order(po_id)
            if order.amount_cents != amount_cents:
                raise bill_reference_mismatch(
                    kind="purchase_order",
                    number=number,
                    vendor_id=vendor_id,
                    reference_id=po_id,
                    invoice_amount_cents=amount_cents,
                    expected_amount_cents=order.amount_cents,
                    currency=currency,
                )
        if gr_id:
            receipt = self.get_goods_receipt(gr_id)
            if receipt.amount_cents != amount_cents:
                raise bill_reference_mismatch(
                    kind="goods_receipt",
                    number=number,
                    vendor_id=vendor_id,
                    reference_id=gr_id,
                    invoice_amount_cents=amount_cents,
                    expected_amount_cents=receipt.amount_cents,
                    currency=currency,
                )
        bill = Bill(
            id=self._next_id("INV", 3001, self._bills),
            number=number,
            vendor_id=vendor_id,
            po_id=po_id,
            gr_id=gr_id,
            amount_cents=amount_cents,
            currency=currency,
            status="received",
            received_at=_now(),
        )
        self._bills[bill.id] = bill
        return bill

    def create_vendor(
        self, *, name: str, tax_id: str, email: str, address: str
    ) -> Vendor:
        existing = [
            vendor
            for vendor in self._sorted(self._vendors)
            if vendor.tax_id == tax_id or vendor.name == name
        ]
        if existing:
            match = next(
                (vendor for vendor in existing if vendor.tax_id == tax_id),
                existing[0],
            )
            matched_on = "tax id" if match.tax_id == tax_id else "name"
            raise duplicate_vendor_error(
                name=name,
                tax_id=tax_id,
                matched_on=matched_on,
                existing_id=match.id,
                existing_status=match.status,
            )
        vendor = Vendor(
            id=self._next_id("V", 1001, self._vendors),
            name=name,
            tax_id=tax_id,
            email=email,
            address=address,
            status="active",
            created_at=_now(),
        )
        self._vendors[vendor.id] = vendor
        return vendor

    def schedule_payment(
        self,
        *,
        bill_id: str,
        amount_cents: int | None = None,
        scheduled_for: str | None = None,
    ) -> Payment:
        bill = self.get_bill(bill_id)
        if amount_cents is not None and amount_cents != bill.amount_cents:
            raise partial_payment_error(bill=bill, amount_cents=amount_cents)
        payment = Payment(
            id=self._next_id("PAY", 1001, self._payments),
            bill_id=bill_id,
            amount_cents=amount_cents or bill.amount_cents,
            currency=bill.currency,
            scheduled_for=scheduled_for
            or (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
            status="scheduled",
        )
        self._payments[payment.id] = payment
        return payment

    @staticmethod
    def _sorted(records: dict):
        return [records[key] for key in sorted(records)]

    @staticmethod
    def _next_id(prefix: str, start: int, records: dict) -> str:
        numbers = [
            int(key.split("-")[1])
            for key in records
            if key.startswith(f"{prefix}-") and key.split("-")[1].isdigit()
        ]
        return f"{prefix}-{max([start - 1, *numbers]) + 1}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
