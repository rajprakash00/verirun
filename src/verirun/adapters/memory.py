"""In-memory mail and accounting fakes for deterministic engine and crash tests.

No database, no network, no Mock Suite. The accounting fake enforces the same
business rules as the Mock Suite adapter: duplicate bills are rejected, bill
amounts must agree with their purchase order and goods receipt, and a payment
covers the whole bill. The mail fake carries one or more messages whose
attachments live under a real document root, so file tools and the Verifier
still read real bytes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from verirun.adapters.base import (
    AccountingSystem,
    Approval,
    Bill,
    GoodsReceipt,
    MailMessage,
    MailSource,
    MailSummary,
    NotFoundError,
    Payment,
    PurchaseOrder,
    TransientError,
    Vendor,
)
from verirun.adapters.rules import (
    bill_reference_mismatch,
    duplicate_bill_error,
    duplicate_vendor_error,
    partial_payment_error,
    reference_marker,
    require_positive_amount,
)


class InMemoryMailSource(MailSource):
    """A mailbox held in dictionaries with attachments under a real root.

    The document root is a real directory because ``invoice.extract`` and the
    Verifier read the attachment bytes themselves. Messages and read state are
    in memory, so a scenario starts from a clean mailbox every time.
    """

    def __init__(
        self,
        document_root: str | Path,
        messages: Iterable[MailMessage] = (),
    ) -> None:
        self._root = Path(document_root)
        self._messages = {message.id: message for message in messages}
        self._read_at: dict[str, str] = {}

    @property
    def document_root(self) -> Path:
        return self._root

    def list_messages(self, folder: str = "inbox") -> list[MailSummary]:
        summaries = [
            MailSummary(
                id=message.id,
                folder=message.folder,
                sender=message.sender,
                sender_email=message.sender_email,
                subject=message.subject,
                received_at=message.received_at,
                attachment_count=len(message.attachments),
                unread=message.id not in self._read_at,
                scenario=message.scenario,
            )
            for message in self._messages.values()
            if message.folder == folder
        ]
        return sorted(
            summaries,
            key=lambda message: (message.received_at, message.id),
            reverse=True,
        )

    def read_message(self, message_id: str) -> MailMessage:
        message = self._messages.get(message_id)
        if message is None:
            raise NotFoundError(f"no message '{message_id}' in the mailbox")
        return message

    def read_attachment(self, message_id: str, attachment_id: str) -> bytes:
        message = self.read_message(message_id)
        attachment = next(
            (item for item in message.attachments if item.id == attachment_id),
            None,
        )
        if attachment is None:
            raise NotFoundError(
                f"no attachment '{attachment_id}' on message '{message_id}'"
            )
        path = self._root / attachment.path
        if not path.is_file():
            raise NotFoundError(f"attachment file not found: {attachment.path}")
        return path.read_bytes()

    def mark_read(self, message_id: str) -> str:
        self.read_message(message_id)
        return self._read_at.setdefault(
            message_id, datetime.now(UTC).isoformat(timespec="seconds")
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
        transient_bill_failures: int = 0,
    ) -> None:
        self._vendors = {item.id: item for item in vendors}
        self._purchase_orders = {item.id: item for item in purchase_orders}
        self._goods_receipts = {item.id: item for item in goods_receipts}
        self._bills = {item.id: item for item in bills}
        self._payments = {item.id: item for item in payments}
        self._approvals = {item.id: item for item in approvals}
        # The next N bill writes fail before landing, so engine tests can prove
        # the transient retry path against the same interface as a real system.
        self._transient_bill_failures = transient_bill_failures

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
        idempotency_key: str | None = None,
    ) -> Bill:
        require_positive_amount(amount_cents)
        if self._transient_bill_failures > 0:
            self._transient_bill_failures -= 1
            raise TransientError(
                "the accounting system is temporarily unavailable; retry the bill",
                data={"transient": True},
            )
        po_id = po_id or None
        gr_id = gr_id or None
        marker = reference_marker(idempotency_key)
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
            if marker and duplicate.reference == marker:
                return duplicate
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
            reference=marker,
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
