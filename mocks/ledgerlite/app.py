from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mocks.ledgerlite import db


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def _parse_amount(raw: str) -> int | None:
    cleaned = raw.strip().lstrip("$").replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    cents = value * 100
    if cents <= 0 or cents != cents.to_integral_value():
        return None
    return int(cents)


def _consume_failure(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT remaining, message FROM failure_flags WHERE key = ?", (key,)
    ).fetchone()
    if row is None or row["remaining"] <= 0:
        return None
    conn.execute("UPDATE failure_flags SET remaining = remaining - 1 WHERE key = ?", (key,))
    conn.commit()
    return row["message"] or "LedgerLite is temporarily unavailable; retry the request."


def _next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE id LIKE ?"
        " ORDER BY CAST(SUBSTR(id, ?) AS INTEGER) DESC LIMIT 1",
        (f"{prefix}-%", len(prefix) + 2),
    ).fetchone()
    number = int(row["id"].split("-")[1]) + 1 if row else 1001
    return f"{prefix}-{number}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _select(
    conn: sqlite3.Connection,
    table: str,
    filters: dict[str, str | None],
) -> list[dict]:
    clauses = []
    params: list[str] = []
    for column, value in filters.items():
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [dict(row) for row in conn.execute(sql, params)]


def _get_one(conn: sqlite3.Connection, table: str, row_id: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} {row_id} not found")
    return dict(row)


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="LedgerLite", version="0.1.0")
    app.state.db_path = Path(db_path)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/vendors")
    def list_vendors(
        conn: Conn,
        status: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(conn, "vendors", {"status": status, "scenario": scenario})

    @app.get("/api/vendors/{vendor_id}")
    def get_vendor(vendor_id: str, conn: Conn) -> dict:
        return _get_one(conn, "vendors", vendor_id)

    @app.get("/api/invoices")
    def list_invoices(
        conn: Conn,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(
            conn,
            "invoices",
            {"number": number, "vendor_id": vendor_id, "status": status, "scenario": scenario},
        )

    @app.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: str, conn: Conn) -> dict:
        return _get_one(conn, "invoices", invoice_id)

    @app.get("/api/purchase-orders")
    def list_purchase_orders(
        conn: Conn,
        vendor_id: str | None = None,
        status: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(
            conn,
            "purchase_orders",
            {"vendor_id": vendor_id, "status": status, "scenario": scenario},
        )

    @app.get("/api/purchase-orders/{po_id}")
    def get_purchase_order(po_id: str, conn: Conn) -> dict:
        return _get_one(conn, "purchase_orders", po_id)

    @app.get("/api/goods-receipts")
    def list_goods_receipts(
        conn: Conn,
        po_id: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(conn, "goods_receipts", {"po_id": po_id, "scenario": scenario})

    @app.get("/api/goods-receipts/{gr_id}")
    def get_goods_receipt(gr_id: str, conn: Conn) -> dict:
        return _get_one(conn, "goods_receipts", gr_id)

    @app.get("/api/payments")
    def list_payments(
        conn: Conn,
        invoice_id: str | None = None,
        status: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(
            conn,
            "payments",
            {"invoice_id": invoice_id, "status": status, "scenario": scenario},
        )

    @app.get("/api/payments/{payment_id}")
    def get_payment(payment_id: str, conn: Conn) -> dict:
        return _get_one(conn, "payments", payment_id)

    @app.get("/api/approvals")
    def list_approvals(
        conn: Conn,
        subject_id: str | None = None,
        status: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(
            conn,
            "approvals",
            {"subject_id": subject_id, "status": status, "scenario": scenario},
        )

    @app.get("/api/approvals/{approval_id}")
    def get_approval(approval_id: str, conn: Conn) -> dict:
        return _get_one(conn, "approvals", approval_id)

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["money"] = _money

    def render(
        request: Request,
        name: str,
        context: dict | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context or {}, status_code=status_code)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: Conn) -> HTMLResponse:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in db.TABLES
        }
        return render(request, "index.html", {"counts": counts})

    @app.get("/invoices", response_class=HTMLResponse)
    def invoices_page(request: Request, conn: Conn) -> HTMLResponse:
        invoices = conn.execute(
            """
            SELECT invoices.*, vendors.name AS vendor_name
            FROM invoices JOIN vendors ON vendors.id = invoices.vendor_id
            ORDER BY invoices.id
            """
        ).fetchall()
        return render(request, "invoices/list.html", {"invoices": invoices})

    @app.get("/invoices/new", response_class=HTMLResponse)
    def new_invoice_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "invoices/new.html",
            {
                "vendors": _select(conn, "vendors", {}),
                "orders": _select(conn, "purchase_orders", {}),
                "receipts": _select(conn, "goods_receipts", {}),
            },
        )

    @app.post("/invoices")
    def file_invoice(
        request: Request,
        conn: Conn,
        number: Annotated[str, Form()],
        vendor_id: Annotated[str, Form()],
        amount: Annotated[str, Form()],
        po_id: Annotated[str, Form()] = "",
        gr_id: Annotated[str, Form()] = "",
        currency: Annotated[str, Form()] = "USD",
    ) -> Response:
        amount_cents = _parse_amount(amount)
        if amount_cents is None:
            return render(
                request,
                "error.html",
                {"status_code": 400, "message": f"Invalid amount: {amount!r}."},
                400,
            )
        if conn.execute("SELECT 1 FROM vendors WHERE id = ?", (vendor_id,)).fetchone() is None:
            return render(
                request,
                "error.html",
                {"status_code": 400, "message": f"Unknown vendor {vendor_id}."},
                400,
            )
        if po_id and conn.execute(
            "SELECT 1 FROM purchase_orders WHERE id = ?", (po_id,)
        ).fetchone() is None:
            return render(
                request,
                "error.html",
                {"status_code": 400, "message": f"Unknown purchase order {po_id}."},
                400,
            )
        if gr_id and conn.execute(
            "SELECT 1 FROM goods_receipts WHERE id = ?", (gr_id,)
        ).fetchone() is None:
            return render(
                request,
                "error.html",
                {"status_code": 400, "message": f"Unknown goods receipt {gr_id}."},
                400,
            )

        failure = _consume_failure(conn, f"invoice.create:{number}")
        if failure is not None:
            return render(request, "error.html", {"status_code": 503, "message": failure}, 503)

        invoice_id = _next_id(conn, "invoices", "INV")
        try:
            conn.execute(
                """
                INSERT INTO invoices
                    (id, number, vendor_id, po_id, gr_id, amount_cents, currency, status,
                     received_at, scenario)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, 'filed')
                """,
                (
                    invoice_id,
                    number,
                    vendor_id,
                    po_id or None,
                    gr_id or None,
                    amount_cents,
                    currency or "USD",
                    _now(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return render(
                request,
                "error.html",
                {
                    "status_code": 409,
                    "message": f"Invoice {number} already exists for {vendor_id}.",
                },
                409,
            )
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
    def invoice_detail_page(request: Request, invoice_id: str, conn: Conn) -> HTMLResponse:
        invoice = conn.execute(
            """
            SELECT invoices.*, vendors.name AS vendor_name
            FROM invoices JOIN vendors ON vendors.id = invoices.vendor_id
            WHERE invoices.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found")
        payments = _select(conn, "payments", {"invoice_id": invoice_id})
        return render(
            request,
            "invoices/detail.html",
            {"invoice": invoice, "payments": payments},
        )

    @app.get("/vendors", response_class=HTMLResponse)
    def vendors_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(request, "vendors/list.html", {"vendors": _select(conn, "vendors", {})})

    @app.get("/vendors/{vendor_id}", response_class=HTMLResponse)
    def vendor_detail_page(request: Request, vendor_id: str, conn: Conn) -> HTMLResponse:
        vendor = _get_one(conn, "vendors", vendor_id)
        orders = _select(conn, "purchase_orders", {"vendor_id": vendor_id})
        return render(request, "vendors/detail.html", {"vendor": vendor, "orders": orders})

    @app.get("/purchase-orders", response_class=HTMLResponse)
    def purchase_orders_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "purchase_orders/list.html",
            {"orders": _select(conn, "purchase_orders", {})},
        )

    @app.get("/purchase-orders/{po_id}", response_class=HTMLResponse)
    def purchase_order_detail_page(request: Request, po_id: str, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "purchase_orders/detail.html",
            {"order": _get_one(conn, "purchase_orders", po_id)},
        )

    @app.get("/goods-receipts", response_class=HTMLResponse)
    def goods_receipts_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "goods_receipts/list.html",
            {"receipts": _select(conn, "goods_receipts", {})},
        )

    @app.get("/goods-receipts/{gr_id}", response_class=HTMLResponse)
    def goods_receipt_detail_page(request: Request, gr_id: str, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "goods_receipts/detail.html",
            {"receipt": _get_one(conn, "goods_receipts", gr_id)},
        )

    @app.get("/payments", response_class=HTMLResponse)
    def payments_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(request, "payments/list.html", {"payments": _select(conn, "payments", {})})

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(
            request,
            "approvals/list.html",
            {"approvals": _select(conn, "approvals", {})},
        )

    return app


DEFAULT_DB = Path("mocks/state/ledgerlite.db")

app = create_app(DEFAULT_DB)
