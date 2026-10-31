"""Contract tests for the mail and accounting adapters.

The Mock Suite adapters and the in-memory fake must behave the same way through
the interface: reads expose the seeded records, writes enforce the same
business rules, and a failure carries the detail the engine's failure policy
needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verirun.adapters import (
    Approval,
    ConflictError,
    GoodsReceipt,
    InMemoryAccountingSystem,
    MockAccountingSystem,
    MockMailSource,
    NotFoundError,
    PurchaseOrder,
    Vendor,
)
from verirun.runtime import Backends

CREATED_AT = "2026-09-01T09:00:00+00:00"

VENDOR = Vendor(
    id="V-1001",
    name="Northwind Traders",
    tax_id="TAX-1001",
    email="billing@northwind.example",
    address="1 North Way",
    status="active",
    created_at=CREATED_AT,
)
PURCHASE_ORDER = PurchaseOrder(
    id="PO-2001",
    vendor_id="V-1001",
    amount_cents=125_000,
    currency="USD",
    status="open",
    issued_at=CREATED_AT,
)
GOODS_RECEIPT = GoodsReceipt(
    id="GR-2501",
    po_id="PO-2001",
    amount_cents=125_000,
    currency="USD",
    status="received",
    received_at=CREATED_AT,
)


def memory_accounting() -> InMemoryAccountingSystem:
    return InMemoryAccountingSystem(
        vendors=[VENDOR],
        purchase_orders=[PURCHASE_ORDER],
        goods_receipts=[GOODS_RECEIPT],
    )


@pytest.fixture(params=["mock", "memory"])
def accounting(request: pytest.FixtureRequest, ledgerlite_db: Path):
    if request.param == "mock":
        return MockAccountingSystem(ledgerlite_db)
    return memory_accounting()


def test_mail_source_lists_reads_and_opens_attachments(maildesk_state) -> None:
    source = MockMailSource(maildesk_state.db_path, maildesk_state.shared_root)

    messages = source.list_messages()
    message = source.read_message("MSG-7001")

    assert len(messages) == 11
    assert messages[0].id == "MSG-7011"
    assert next(item for item in messages if item.id == "MSG-7001").attachment_count == 7
    assert source.document_root == maildesk_state.shared_root
    assert message.subject == "Invoice batch for processing — September"
    assert len(message.attachments) == 7
    attachment = message.attachments[0]
    assert source.read_attachment(message.id, attachment.id).startswith(b"%PDF")
    assert (maildesk_state.shared_root / attachment.path).is_file()


def test_mail_source_marks_a_message_read(maildesk_state) -> None:
    source = MockMailSource(maildesk_state.db_path, maildesk_state.shared_root)

    source.mark_read("MSG-7002")
    message = source.read_message("MSG-7002")
    unread = next(item for item in source.list_messages() if item.id == "MSG-7002")

    assert message.id == "MSG-7002"
    assert unread.unread is False
    assert source.mark_read("MSG-7002")


def test_mail_source_reports_an_unknown_message(maildesk_state) -> None:
    source = MockMailSource(maildesk_state.db_path, maildesk_state.shared_root)

    with pytest.raises(NotFoundError):
        source.read_message("MSG-9999")
    with pytest.raises(NotFoundError):
        source.mark_read("MSG-9999")


def test_accounting_reads_expose_vendors_orders_and_receipts(accounting) -> None:
    vendor = accounting.get_vendor("V-1001")
    orders = accounting.list_purchase_orders(vendor_id="V-1001")
    order = accounting.get_purchase_order("PO-2001")
    receipt = accounting.get_goods_receipt("GR-2501")
    named = accounting.find_vendors(name="Northwind Traders")
    by_tax_id = accounting.find_vendors(tax_id="TAX-1001")

    assert vendor.name == "Northwind Traders"
    assert [item.id for item in orders] == ["PO-2001"]
    assert order.amount_cents == 125_000
    assert receipt.po_id == "PO-2001"
    assert [item.id for item in named] == ["V-1001"]
    assert [item.id for item in by_tax_id] == ["V-1001"]
    with pytest.raises(NotFoundError):
        accounting.get_vendor("V-9999")


def test_accounting_creates_one_purchase_bill_and_rejects_the_duplicate(
    accounting,
) -> None:
    bill = accounting.create_purchase_bill(
        number="NW-2026-001",
        vendor_id="V-1001",
        amount_cents=125_000,
        po_id="PO-2001",
        gr_id="GR-2501",
    )

    assert bill.status == "received"
    assert bill.amount_cents == 125_000
    assert [item.id for item in accounting.list_bills(number="NW-2026-001")] == [bill.id]

    with pytest.raises(ConflictError) as excinfo:
        accounting.create_purchase_bill(
            number="NW-2026-001", vendor_id="V-1001", amount_cents=125_000
        )

    assert excinfo.value.data["duplicate"] is True
    assert excinfo.value.data["existing_invoice_id"] == bill.id


def test_accounting_rejects_an_amount_that_does_not_match_the_order(accounting) -> None:
    with pytest.raises(ConflictError) as excinfo:
        accounting.create_purchase_bill(
            number="NW-2026-002",
            vendor_id="V-1001",
            amount_cents=100_000,
            po_id="PO-2001",
        )

    assert excinfo.value.data["mismatch"] == "purchase_order"
    assert excinfo.value.data["expected_amount_cents"] == 125_000


def test_accounting_creates_a_vendor_once(accounting) -> None:
    vendor = accounting.create_vendor(
        name="Cascade Fabrication LLC",
        tax_id="TAX-2001",
        email="accounts@cascade.example",
        address="4820 Foundry Way",
    )

    assert vendor.status == "active"
    assert accounting.get_vendor(vendor.id).tax_id == "TAX-2001"

    with pytest.raises(ConflictError) as excinfo:
        accounting.create_vendor(
            name="Cascade Fabrication",
            tax_id="TAX-2001",
            email="other@cascade.example",
            address="1 Other Way",
        )

    assert excinfo.value.data["existing_vendor_id"] == vendor.id


def test_accounting_schedules_a_full_payment_and_rejects_a_partial(accounting) -> None:
    bill = accounting.create_purchase_bill(
        number="NW-2026-003", vendor_id="V-1001", amount_cents=125_000
    )

    with pytest.raises(ConflictError) as excinfo:
        accounting.schedule_payment(bill_id=bill.id, amount_cents=100_000)
    assert "splitting is forbidden" in str(excinfo.value)

    payment = accounting.schedule_payment(
        bill_id=bill.id, amount_cents=125_000, scheduled_for="2026-10-05"
    )

    assert payment.status == "scheduled"
    assert payment.bill_id == bill.id
    assert [item.id for item in accounting.list_payments(bill.id)] == [payment.id]


def test_accounting_rejects_a_non_positive_bill_amount(accounting) -> None:
    with pytest.raises(ConflictError):
        accounting.create_purchase_bill(
            number="NW-2026-004", vendor_id="V-1001", amount_cents=0
        )


def test_accounting_treats_an_empty_reference_as_absent(accounting) -> None:
    bill = accounting.create_purchase_bill(
        number="NW-2026-005", vendor_id="V-1001", amount_cents=125_000, po_id=""
    )

    assert bill.po_id is None


def test_accounting_lists_vendors_when_no_filter_is_given(accounting) -> None:
    vendors = accounting.find_vendors()

    assert "V-1001" in {vendor.id for vendor in vendors}


def test_backends_prefer_the_read_only_accounting_adapter(tmp_path: Path) -> None:
    write = memory_accounting()
    read = memory_accounting()
    backends = Backends(
        mail=MockMailSource(tmp_path / "maildesk.db", tmp_path),
        accounting=write,
        reads=read,
    )

    assert backends.verifier_accounting is read


def test_in_memory_accounting_finds_a_pending_approval() -> None:
    fake = InMemoryAccountingSystem(
        vendors=[VENDOR],
        purchase_orders=[PURCHASE_ORDER],
        goods_receipts=[GOODS_RECEIPT],
        approvals=[
            Approval(
                id="AP-1",
                subject_type="invoice",
                subject_id="INV-3001",
                kind="payment",
                status="pending",
                requested_at=CREATED_AT,
                note="Above the limit",
            )
        ],
    )

    approval = fake.find_pending_approval("INV-3001")

    assert approval is not None
    assert approval.id == "AP-1"
    assert fake.find_pending_approval("INV-9999") is None
