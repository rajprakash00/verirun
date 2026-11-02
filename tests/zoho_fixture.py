"""Recorded Zoho Books India responses shared by the adapter and end-to-end tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "zoho"
VENDOR_ID = "4600000000001"
PO_ID = "4600000000101"
BILL_ID = "4600000000201"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def recorded(request: httpx.Request) -> httpx.Response:
    """Serve the recorded Zoho trial responses by URL and page number."""
    if request.url.host == "accounts.zoho.in":
        return httpx.Response(200, json=fixture("token.json"))
    path = request.url.path
    if path.endswith("/contacts"):
        page = request.url.params.get("page", "1")
        return httpx.Response(200, json=fixture(f"contacts-page-{page}.json"))
    if path.endswith(f"/contacts/{VENDOR_ID}"):
        return httpx.Response(200, json=fixture("contact.json"))
    if path.endswith("/purchaseorders"):
        return httpx.Response(200, json=fixture("purchaseorders-page-1.json"))
    if path.endswith(f"/purchaseorders/{PO_ID}"):
        return httpx.Response(200, json=fixture("purchaseorder.json"))
    if path.endswith("/bills"):
        return httpx.Response(200, json=fixture("bills-page-1.json"))
    if path.endswith(f"/bills/{BILL_ID}"):
        return httpx.Response(200, json=fixture("bill.json"))
    if path.endswith("/vendorpayments"):
        return httpx.Response(200, json=fixture("vendorpayments-page-1.json"))
    return httpx.Response(404, json={"code": 1002, "message": "Record not found"})
