import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from mocks.ledgerlite.app import create_app
from mocks.seed.ledgerlite import reset_and_seed


def test_seeded_pages_render(client: TestClient) -> None:
    invoices = client.get("/invoices")
    assert invoices.status_code == 200
    assert "NW-2025-118" in invoices.text
    assert "AQ-2026-014" in invoices.text

    detail = client.get("/invoices/INV-3002")
    assert detail.status_code == 200
    assert "Apex Office Supplies" in detail.text
    assert "paid" in detail.text

    vendors = client.get("/vendors")
    assert vendors.status_code == 200
    assert "Blocked Supplies Co" in vendors.text

    vendor_detail = client.get("/vendors/V-1006")
    assert vendor_detail.status_code == 200
    assert "blocked" in vendor_detail.text

    for path, marker in (
        ("/purchase-orders", "PO-2001"),
        ("/goods-receipts", "GR-2501"),
        ("/payments", "PAY-4001"),
        ("/approvals", "AP-6001"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path


def test_new_invoice_form_renders_vendors(client: TestClient) -> None:
    response = client.get("/invoices/new")

    assert response.status_code == 200
    assert "Northwind Traders" in response.text
    assert "PO-2001" in response.text


def test_new_invoice_form_writes_through_to_sqlite(
    client: TestClient, ledgerlite_db: Path
) -> None:
    response = client.post(
        "/invoices",
        data={
            "number": "NW-2026-999",
            "vendor_id": "V-1001",
            "po_id": "PO-2001",
            "gr_id": "GR-2501",
            "amount": "1250.00",
            "currency": "USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with closing(sqlite3.connect(ledgerlite_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM invoices WHERE number = ?", ("NW-2026-999",)
        ).fetchone()
    assert row is not None
    assert row["vendor_id"] == "V-1001"
    assert row["po_id"] == "PO-2001"
    assert row["gr_id"] == "GR-2501"
    assert row["amount_cents"] == 125000
    assert row["status"] == "received"


def test_new_invoice_form_rejects_unknown_vendor(client: TestClient) -> None:
    response = client.post(
        "/invoices",
        data={"number": "XX-1", "vendor_id": "V-9999", "amount": "10.00"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "V-9999" in response.text


def test_new_invoice_form_rejects_a_duplicate_number(client: TestClient) -> None:
    response = client.post(
        "/invoices",
        data={
            "number": "AQ-2026-014",
            "vendor_id": "V-1002",
            "po_id": "PO-2002",
            "gr_id": "GR-2502",
            "amount": "640.00",
            "currency": "USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409


def test_transient_invoice_write_fails_once_then_succeeds(client: TestClient) -> None:
    payload = {
        "number": "RS-2026-088",
        "vendor_id": "V-1007",
        "po_id": "PO-2007",
        "gr_id": "GR-2507",
        "amount": "890.00",
        "currency": "USD",
    }

    first = client.post("/invoices", data=payload, follow_redirects=False)
    assert first.status_code == 503
    assert "transiently" in first.text
    assert client.get("/api/invoices", params={"number": "RS-2026-088"}).json() == []

    second = client.post("/invoices", data=payload, follow_redirects=False)
    assert second.status_code == 303
    assert len(client.get("/api/invoices", params={"number": "RS-2026-088"}).json()) == 1


def test_transient_flag_survives_an_invalid_first_attempt(client: TestClient) -> None:
    invalid = {
        "number": "RS-2026-088",
        "vendor_id": "V-1007",
        "po_id": "PO-2007",
        "gr_id": "GR-2507",
        "amount": "not-a-number",
        "currency": "USD",
    }
    valid = {**invalid, "amount": "890.00"}

    assert client.post("/invoices", data=invalid, follow_redirects=False).status_code == 400
    assert client.post("/invoices", data=valid, follow_redirects=False).status_code == 503
    assert client.post("/invoices", data=valid, follow_redirects=False).status_code == 303


def test_reseeding_restores_the_transient_failure_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "ledgerlite.db"
    payload = {
        "number": "RS-2026-088",
        "vendor_id": "V-1007",
        "po_id": "PO-2007",
        "gr_id": "GR-2507",
        "amount": "890.00",
        "currency": "USD",
    }

    reset_and_seed(db_path)
    with TestClient(create_app(db_path)) as first_run:
        assert first_run.post("/invoices", data=payload).status_code == 503

    reset_and_seed(db_path)
    with TestClient(create_app(db_path)) as second_run:
        assert second_run.post("/invoices", data=payload).status_code == 503
