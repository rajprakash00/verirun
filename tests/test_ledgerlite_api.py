from fastapi.testclient import TestClient


def test_api_returns_seeded_vendors(client: TestClient) -> None:
    response = client.get("/api/vendors")

    assert response.status_code == 200
    vendors = {vendor["id"]: vendor for vendor in response.json()}
    assert len(vendors) == 9
    assert vendors["V-1001"]["name"] == "Northwind Traders"
    assert vendors["V-1000"]["scenario"] == "history"
    assert vendors["V-1006"]["status"] == "blocked"
    assert vendors["V-1004"]["scenario"] == "missing_po"


def test_api_returns_seeded_purchase_orders_and_goods_receipts(client: TestClient) -> None:
    orders = client.get("/api/purchase-orders").json()
    receipts = client.get("/api/goods-receipts").json()

    assert len(orders) == 8
    order = next(row for row in orders if row["id"] == "PO-2001")
    assert order["vendor_id"] == "V-1001"
    assert order["amount_cents"] == 125000

    assert len(receipts) == 8
    receipt = next(row for row in receipts if row["id"] == "GR-2501")
    assert receipt["po_id"] == "PO-2001"
    assert receipt["amount_cents"] == 125000


def test_api_returns_seeded_invoices_and_payments(client: TestClient) -> None:
    invoices = client.get("/api/invoices").json()
    payments = client.get("/api/payments").json()

    assert {row["id"] for row in invoices} == {"INV-3001", "INV-3002"}
    duplicate = next(row for row in invoices if row["number"] == "AQ-2026-014")
    assert duplicate["id"] == "INV-3002"
    assert duplicate["status"] == "paid"
    assert {row["vendor_id"] for row in invoices} == {"V-1000", "V-1002"}
    assert {row["id"] for row in payments} == {"PAY-4001", "PAY-4002"}


def test_api_finds_invoice_by_number(client: TestClient) -> None:
    response = client.get("/api/invoices", params={"number": "AQ-2026-014"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["INV-3002"]

    empty = client.get("/api/invoices", params={"number": "NW-2026-001"})
    assert empty.json() == []


def test_api_returns_seeded_approvals(client: TestClient) -> None:
    approvals = client.get("/api/approvals").json()

    assert [row["id"] for row in approvals] == ["AP-6001", "AP-6002"]
    assert approvals[0]["status"] == "approved"


def test_api_detail_reports_missing_rows(client: TestClient) -> None:
    assert client.get("/api/invoices/INV-9999").status_code == 404
    assert client.get("/api/vendors/V-9999").status_code == 404
