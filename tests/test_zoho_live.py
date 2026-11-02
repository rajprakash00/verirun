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
from uuid import uuid4

import pytest

from verirun.adapters.zoho import (
    ZohoAccountingReads,
    ZohoBooksClient,
    ZohoOAuthConfig,
    ZohoTokenManager,
    ZohoTokenStore,
)
from verirun.context.task_pack import TaskPack, VerificationCheck
from verirun.engine.models import Observation
from verirun.engine.states import RunState
from verirun.engine.verify import verify_run
from verirun.runs.store import RunStore

REQUIRED = (
    "VERIRUN_ZOHO_LIVE",
    "VERIRUN_ZOHO_CLIENT_ID",
    "VERIRUN_ZOHO_CLIENT_SECRET",
    "VERIRUN_ZOHO_READ_REFRESH_TOKEN",
    "VERIRUN_ZOHO_ORGANIZATION_ID",
    "VERIRUN_ZOHO_TOKEN_KEY",
)

pytestmark = pytest.mark.skipif(
    os.environ.get("VERIRUN_ZOHO_LIVE") != "1"
    or not all(os.environ.get(name) for name in REQUIRED),
    reason="set VERIRUN_ZOHO_LIVE=1 and the VERIRUN_ZOHO_* read-only credentials",
)


def reads(tmp_path: Path) -> tuple[ZohoAccountingReads, ZohoTokenStore]:
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
    return ZohoAccountingReads(client), store


def test_the_read_only_client_reaches_vendors_purchase_orders_and_bills(
    tmp_path: Path,
) -> None:
    accounting, store = reads(tmp_path)

    vendors = accounting.list_vendors()
    orders = accounting.list_purchase_orders()
    bills = accounting.list_bills()

    assert all(vendor.id for vendor in vendors)
    assert all(order.id and order.amount_cents >= 0 for order in orders)
    assert all(bill.id and bill.vendor_id for bill in bills)
    assert store.path.is_file()


def test_a_real_vendor_and_purchase_order_produce_verification_evidence(
    tmp_path: Path,
) -> None:
    accounting, _ = reads(tmp_path)
    vendors = [
        vendor
        for vendor in accounting.list_vendors(status="active")
        if vendor.tax_id
    ]
    if not vendors:
        pytest.skip("the trial has no active vendor with a GSTIN")
    vendor = vendors[0]
    orders = accounting.list_purchase_orders(vendor_id=vendor.id, status="open")
    if not orders:
        pytest.skip("the trial has no open purchase order for the first vendor")
    order = orders[0]
    reference = order.number or order.id

    store = RunStore(tmp_path / "runs.db")
    run_id = "RUN-ZOHO-LIVE"
    store.create_run("Match a trial invoice read-only", "zoho-live", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)
    store.transition(run_id, RunState.VERIFYING)
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Live contract extraction",
            data={
                "parsed": {
                    "supplier_name": vendor.name,
                    "supplier_gstin": vendor.tax_id,
                    "invoice_number": f"VERIRUN-LIVE-{uuid4().hex[:10]}",
                    "total_paise": order.amount_cents,
                    "currency": order.currency,
                    "po_reference": reference,
                },
                "fields": {"po_reference": {"value": reference}},
            },
        ),
        tool="invoice.extract",
    )
    pack = TaskPack(
        id="zoho-live",
        goal_template="Match a trial invoice read-only",
        sop="india-gst-extraction",
        tools=["invoice.extract"],
        extraction={"confidence_threshold": 0.8},
        verification=[
            VerificationCheck(
                id="accounting-invoice-matches",
                description="Match the trial vendor and purchase order read-only.",
                check="accounting_invoice_matches",
            )
        ],
    )

    run = verify_run(
        run_id, pack, store, accounting=accounting, shared_root=tmp_path
    )

    assert run.state is RunState.COMPLETED, run.error
    result = store.get_verification(run_id)[0]
    assert f"accounting:vendor:{vendor.id}" in result.evidence
    assert f"accounting:purchaseorder:{order.id}" in result.evidence
