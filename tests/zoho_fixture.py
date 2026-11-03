"""Recorded Zoho Books India responses shared by the adapter and end-to-end tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "zoho"
VENDOR_ID = "4600000000001"
PO_ID = "4600000000101"
BILL_ID = "4600000000201"


class FakeTokens:
    """A token manager stand-in for the client and read adapter tests."""

    def __init__(self) -> None:
        self.requests = 0
        self.forced = 0

    def access_token(self, *, force: bool = False) -> str:
        if force:
            self.forced += 1
            return "fresh-token"
        self.requests += 1
        return "access-token"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ZohoBooksFake:
    """A stateful Zoho Books stub for the write path.

    GET /bills serves the recorded bills plus anything this fake created. POST
    /bills records the body, creates a bill, and can fail before or after the
    create to exercise reconciliation.
    """

    def __init__(
        self,
        *,
        fail_post_before_create: bool = False,
        fail_post_after_create: bool = False,
    ) -> None:
        self.bills = list(fixture("bills-page-1.json")["bills"])
        self.posts: list[dict] = []
        self.fail_post_before_create = fail_post_before_create
        self.fail_post_after_create = fail_post_after_create

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/bills"):
            body = json.loads(request.content)
            self.posts.append(body)
            if self.fail_post_before_create:
                return httpx.Response(503, json={"code": 9999, "message": "busy"})
            bill = {
                "bill_id": f"460000000{210 + len(self.posts)}",
                "bill_number": body["bill_number"],
                "vendor_id": body["vendor_id"],
                "total": body["line_items"][0]["rate"],
                "currency_code": body.get("currency_code", "INR"),
                "status": "open",
                "date": body["date"],
                "reference_number": body.get("reference_number", ""),
                "purchaseorder_ids": body.get("purchaseorder_ids", []),
            }
            self.bills.append(bill)
            if self.fail_post_after_create:
                return httpx.Response(503, json={"code": 9999, "message": "busy"})
            return httpx.Response(201, json={"code": 0, "message": "success", "bill": bill})
        if request.method == "GET" and request.url.path.endswith("/bills"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "page_context": {
                        "page": 1,
                        "per_page": 200,
                        "has_more_page": False,
                    },
                    "bills": self.bills,
                },
            )
        return recorded(request)


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
