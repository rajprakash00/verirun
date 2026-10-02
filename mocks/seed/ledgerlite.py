"""Reset and seed LedgerLite's SQLite ground truth.

Deterministic by construction: every id, amount, and timestamp is a literal,
and resetting deletes the database file before recreating it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from mocks.ledgerlite import db
from mocks.seed import scenarios

HISTORY_AT = "2026-08-20T09:00:00+00:00"
PO_ISSUED_AT = "2026-09-01T09:00:00+00:00"
GR_RECEIVED_AT = "2026-09-03T09:00:00+00:00"
INVOICE_RECEIVED_AT = "2026-09-05T09:00:00+00:00"
PAYMENT_SCHEDULED_FOR = "2026-09-30"
APPROVAL_REQUESTED_AT = "2026-08-25T09:00:00+00:00"
APPROVAL_DECIDED_AT = "2026-08-26T09:00:00+00:00"

TRANSIENT_FAILURE_KEY = f"invoice.create:{scenarios.TRANSIENT.invoice_number}"

HISTORY_VENDOR_ID = "V-1000"
HISTORY_PO_ID = "PO-2000"
HISTORY_GR_ID = "GR-2500"


def reset_and_seed(db_path: Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    with closing(db.connect(db_path)) as conn:
        db.initialize(conn)
        seed(conn)
        conn.commit()


def seed(conn: sqlite3.Connection) -> None:
    for scenario in scenarios.SCENARIOS:
        conn.execute(
            """
            INSERT INTO vendors (id, name, tax_id, email, status, created_at, scenario)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scenario.vendor_id,
                scenario.vendor_name,
                scenario.vendor_tax_id,
                scenario.vendor_email,
                scenario.vendor_status,
                HISTORY_AT,
                scenario.key,
            ),
        )
        if scenario.po_id is not None:
            conn.execute(
                """
                INSERT INTO purchase_orders
                    (id, vendor_id, amount_cents, status, issued_at, scenario)
                VALUES (?, ?, ?, 'open', ?, ?)
                """,
                (
                    scenario.po_id,
                    scenario.vendor_id,
                    scenario.po_amount_cents,
                    PO_ISSUED_AT,
                    scenario.key,
                ),
            )
        if scenario.gr_id is not None:
            conn.execute(
                """
                INSERT INTO goods_receipts
                    (id, po_id, amount_cents, status, received_at, scenario)
                VALUES (?, ?, ?, 'received', ?, ?)
                """,
                (
                    scenario.gr_id,
                    scenario.po_id,
                    scenario.gr_amount_cents,
                    GR_RECEIVED_AT,
                    scenario.key,
                ),
            )

    conn.execute(
        """
        INSERT INTO vendors (id, name, tax_id, email, status, created_at, scenario)
        VALUES (?, ?, ?, ?, 'active', ?, 'history')
        """,
        (HISTORY_VENDOR_ID, "Contoso Industrial", "TAX-1000", "ap@contoso.example", HISTORY_AT),
    )
    conn.execute(
        """
        INSERT INTO purchase_orders (id, vendor_id, amount_cents, status, issued_at, scenario)
        VALUES (?, ?, 98000, 'closed', ?, 'history')
        """,
        (HISTORY_PO_ID, HISTORY_VENDOR_ID, PO_ISSUED_AT),
    )
    conn.execute(
        """
        INSERT INTO goods_receipts (id, po_id, amount_cents, status, received_at, scenario)
        VALUES (?, ?, 98000, 'received', ?, 'history')
        """,
        (HISTORY_GR_ID, HISTORY_PO_ID, GR_RECEIVED_AT),
    )
    _insert_invoice(
        conn,
        invoice_id="INV-3001",
        number="NW-2025-118",
        vendor_id=HISTORY_VENDOR_ID,
        po_id=HISTORY_PO_ID,
        gr_id=HISTORY_GR_ID,
        amount_cents=98000,
        status="paid",
        scenario="history",
    )
    _insert_invoice(
        conn,
        invoice_id=scenarios.DUPLICATE.existing_invoice_id,
        number=scenarios.DUPLICATE.invoice_number,
        vendor_id=scenarios.DUPLICATE.vendor_id,
        po_id=scenarios.DUPLICATE.po_id,
        gr_id=scenarios.DUPLICATE.gr_id,
        amount_cents=scenarios.DUPLICATE.invoice_amount_cents,
        status="paid",
        scenario=scenarios.DUPLICATE.key,
    )
    _insert_payment(
        conn,
        payment_id="PAY-4001",
        invoice_id="INV-3001",
        amount_cents=98000,
        status="paid",
        scenario="history",
    )
    _insert_payment(
        conn,
        payment_id="PAY-4002",
        invoice_id=scenarios.DUPLICATE.existing_invoice_id,
        amount_cents=scenarios.DUPLICATE.invoice_amount_cents,
        status="paid",
        scenario=scenarios.DUPLICATE.key,
    )
    conn.execute(
        """
        INSERT INTO approvals
            (id, subject_type, subject_id, kind, status, requested_at, decided_at, note, scenario)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "AP-6001",
            "purchase_order",
            scenarios.HAPPY.po_id,
            "spend",
            "approved",
            APPROVAL_REQUESTED_AT,
            APPROVAL_DECIDED_AT,
            "Approved in the September procurement review.",
            "history",
        ),
    )
    conn.execute(
        """
        INSERT INTO approvals
            (id, subject_type, subject_id, kind, status, requested_at, decided_at, note, scenario)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "AP-6002",
            "invoice",
            scenarios.DUPLICATE.existing_invoice_id,
            "payment",
            "approved",
            APPROVAL_REQUESTED_AT,
            APPROVAL_DECIDED_AT,
            "Paid in the September payment run.",
            scenarios.DUPLICATE.key,
        ),
    )
    conn.execute(
        "INSERT INTO failure_flags (key, remaining, message) VALUES (?, ?, ?)",
        (
            TRANSIENT_FAILURE_KEY,
            1,
            "LedgerLite write failed transiently; retry the request.",
        ),
    )


def _insert_invoice(
    conn: sqlite3.Connection,
    *,
    invoice_id: str,
    number: str,
    vendor_id: str,
    po_id: str | None,
    gr_id: str | None,
    amount_cents: int,
    status: str,
    scenario: str,
) -> None:
    conn.execute(
        """
        INSERT INTO invoices
            (id, number, vendor_id, po_id, gr_id, amount_cents, status, received_at, scenario)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_id,
            number,
            vendor_id,
            po_id,
            gr_id,
            amount_cents,
            status,
            INVOICE_RECEIVED_AT,
            scenario,
        ),
    )


def _insert_payment(
    conn: sqlite3.Connection,
    *,
    payment_id: str,
    invoice_id: str,
    amount_cents: int,
    status: str,
    scenario: str,
) -> None:
    conn.execute(
        """
        INSERT INTO payments
            (id, invoice_id, amount_cents, scheduled_for, status, scenario)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (payment_id, invoice_id, amount_cents, PAYMENT_SCHEDULED_FOR, status, scenario),
    )
