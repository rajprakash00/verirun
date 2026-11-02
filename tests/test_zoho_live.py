"""Opt-in contract test against a real Zoho Books India trial.

CI has no Zoho credentials, so this test skips unless the operator exports a
read-only OAuth application. Run it before a pilot with the same scopes the
product uses:

    ZohoBooks.contacts.READ
    ZohoBooks.purchaseorders.READ
    ZohoBooks.bills.READ
    ZohoBooks.vendorpayments.READ

It never creates a bill.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoBooksClient,
    ZohoOAuthConfig,
    ZohoTokenManager,
    ZohoTokenStore,
)

REQUIRED = (
    "VERIRUN_ZOHO_CLIENT_ID",
    "VERIRUN_ZOHO_CLIENT_SECRET",
    "VERIRUN_ZOHO_READ_REFRESH_TOKEN",
    "VERIRUN_ZOHO_ORGANIZATION_ID",
    "VERIRUN_ZOHO_TOKEN_KEY",
)

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED),
    reason="set the VERIRUN_ZOHO_* read-only credentials to run the live contract",
)


def test_the_read_only_client_reaches_vendors_purchase_orders_and_bills(
    tmp_path: Path,
) -> None:
    oauth = ZohoOAuthConfig(
        client_id=os.environ["VERIRUN_ZOHO_CLIENT_ID"],
        client_secret=os.environ["VERIRUN_ZOHO_CLIENT_SECRET"],
        refresh_token=os.environ["VERIRUN_ZOHO_READ_REFRESH_TOKEN"],
        accounts_domain=os.environ.get(
            "VERIRUN_ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.in"
        ),
    )
    store = ZohoTokenStore(
        tmp_path / "zoho-read-token.enc", os.environ["VERIRUN_ZOHO_TOKEN_KEY"]
    )
    client = ZohoBooksClient(
        token_manager=ZohoTokenManager(oauth, store),
        organization_id=os.environ["VERIRUN_ZOHO_ORGANIZATION_ID"],
        base_url=os.environ.get(
            "VERIRUN_ZOHO_API_BASE_URL", "https://www.zohoapis.in/books/v3"
        ),
    )
    reads = ZohoAccountingReads(client)

    vendors = reads.list_vendors()
    orders = reads.list_purchase_orders()
    bills = reads.list_bills()

    assert all(vendor.id for vendor in vendors)
    assert all(order.id and order.amount_cents >= 0 for order in orders)
    assert all(bill.id and bill.vendor_id for bill in bills)
    assert store.path.is_file()
