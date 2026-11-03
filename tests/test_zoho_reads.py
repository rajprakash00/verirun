"""The Zoho Books read adapter: vendor, PO, bill, and payment matching.

The responses are recorded Zoho Books India fixtures, so the tests assert the
adapter's public behaviour without a live trial: pagination across contacts,
GSTIN and name lookup, duplicate detection by vendor and bill number, and the
status vocabulary the engine expects.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.zoho_fixture import PO_ID, VENDOR_ID, FakeTokens, fixture, recorded
from verirun.adapters import (
    NotFoundError,
    UnsupportedError,
)
from verirun.adapters.zoho import ZohoAccountingReads, ZohoAccountingSystem, ZohoBooksClient

GSTIN = "29AABCV1234C1ZQ"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def zoho_reads(handler=recorded, *, sleep=None, clock=None) -> ZohoAccountingReads:
    client = ZohoBooksClient(
        token_manager=FakeTokens(),  # type: ignore[arg-type]
        organization_id="6000000000001",
        base_url="https://www.zohoapis.in/books/v3",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
    )
    return ZohoAccountingReads(
        client,
        sleep=sleep or (lambda seconds: None),
        clock=clock or time.monotonic,
    )


def test_vendors_are_read_across_pages_and_matched_by_gstin_and_name() -> None:
    reads = zoho_reads()

    vendors = reads.list_vendors()
    by_gstin = reads.find_vendors(tax_id=GSTIN)
    by_name = reads.find_vendors(name="Paper Trail Supplies Pvt Ltd")
    vendor = reads.get_vendor(VENDOR_ID)

    assert [item.id for item in vendors] == ["4600000000001", "4600000000002"]
    assert [item.id for item in by_gstin] == [VENDOR_ID]
    assert [item.id for item in by_name] == [VENDOR_ID]
    assert vendor.tax_id == GSTIN
    assert vendor.name == "Paper Trail Supplies Pvt. Ltd."
    assert vendor.email == "accounts@papertrail.example"
    assert vendor.address.startswith("14 Market Road")
    assert vendors[1].status == "inactive"


def test_a_missing_vendor_is_not_found() -> None:
    reads = zoho_reads()

    with pytest.raises(NotFoundError):
        reads.get_vendor("999")


def test_purchase_orders_map_their_number_amount_and_status() -> None:
    reads = zoho_reads()

    orders = reads.list_purchase_orders(vendor_id=VENDOR_ID)
    order = reads.get_purchase_order(PO_ID)

    assert [item.id for item in orders] == [PO_ID]
    assert order.number == "PO-IN-2001"
    assert order.vendor_id == VENDOR_ID
    assert order.amount_cents == 1_180_000
    assert order.currency == "INR"
    assert order.status == "open"
    assert order.issued_at == "2026-09-01"


def test_bills_are_matched_by_vendor_and_invoice_number() -> None:
    reads = zoho_reads()

    duplicate = reads.list_bills(number="PTS/2026/0141", vendor_id=VENDOR_ID)
    new_invoice = reads.list_bills(number="PTS/2026/0142", vendor_id=VENDOR_ID)

    assert [item.id for item in duplicate] == ["4600000000201"]
    assert new_invoice == []
    assert duplicate[0].po_id == PO_ID
    assert duplicate[0].amount_cents == 500_000


def test_bill_statuses_are_normalised_for_the_engine() -> None:
    reads = zoho_reads()

    assert [item.id for item in reads.list_bills(status="approved")] == ["4600000000201"]
    assert [item.id for item in reads.list_bills(status="paid")] == ["4600000000202"]
    assert [item.id for item in reads.list_bills(status="rejected")] == ["4600000000203"]


def test_a_bill_is_fetched_by_id() -> None:
    reads = zoho_reads()

    bill = reads.get_bill("4600000000201")

    assert bill.number == "PTS/2026/0141"
    assert bill.status == "approved"


def test_payments_are_read_for_a_bill() -> None:
    reads = zoho_reads()

    payments = reads.list_payments("4600000000201")

    assert [item.id for item in payments] == ["4600000000301"]
    assert payments[0].amount_cents == 500_000
    assert payments[0].status == "paid"


def test_zoho_books_records_no_goods_receipts() -> None:
    with pytest.raises(NotFoundError, match="goods receipt"):
        zoho_reads().get_goods_receipt("GR-1")


def test_zoho_books_has_no_pending_bill_approval() -> None:
    assert zoho_reads().find_pending_approval("4600000000201") is None


def test_a_bill_is_polled_until_zoho_returns_it() -> None:
    clock = FakeClock()
    sleeps: list[float] = []
    empty = {
        "code": 0,
        "message": "success",
        "page_context": {
            "page": 1,
            "per_page": 200,
            "has_more_page": False,
        },
        "bills": [],
    }
    responses = iter([empty, empty, fixture("bills-page-1.json")])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bills"):
            return httpx.Response(200, json=next(responses))
        return recorded(request)

    reads = zoho_reads(handler, sleep=sleeps.append, clock=clock)

    bill = reads.wait_for_bill(
        number="PTS/2026/0141", vendor_id=VENDOR_ID, timeout_s=10, interval_s=1
    )

    assert bill.id == "4600000000201"
    assert sleeps == [1.0, 1.0]


def test_polling_for_a_bill_times_out_as_not_found() -> None:
    clock = FakeClock()
    sleeps: list[float] = []
    empty = {
        "code": 0,
        "message": "success",
        "page_context": {"page": 1, "per_page": 200, "has_more_page": False},
        "bills": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=empty)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    reads = zoho_reads(handler, sleep=sleep, clock=clock)

    with pytest.raises(NotFoundError, match="has not returned"):
        reads.wait_for_bill(
            number="PTS/2026/0142", vendor_id=VENDOR_ID, timeout_s=2, interval_s=1
        )
    assert sleeps == [1.0, 1.0]


def test_the_write_path_is_explicitly_unsupported_until_the_next_ticket() -> None:
    accounting = ZohoAccountingSystem(zoho_reads())

    with pytest.raises(UnsupportedError):
        accounting.create_purchase_bill(
            number="PTS/2026/0142", vendor_id=VENDOR_ID, amount_cents=1_180_000
        )
