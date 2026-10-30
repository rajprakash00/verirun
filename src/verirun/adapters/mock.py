"""The Mock Suite adapters: MailDesk and LedgerLite.

They implement the product interfaces over the deterministic SQLite fixtures
the demo, eval, and tests use. Nothing here is product-path code: the real IMAP
and Zoho Books adapters are swaps behind the same interfaces.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from verirun.adapters.base import (
    AccountingSystem,
    Approval,
    Bill,
    GoodsReceipt,
    MailAttachment,
    MailMessage,
    MailSource,
    MailSummary,
    NotFoundError,
    Payment,
    PurchaseOrder,
    TransientError,
    Vendor,
)
from verirun.adapters.rules import (
    bill_reference_mismatch,
    duplicate_bill_error,
    duplicate_vendor_error,
    partial_payment_error,
    require_positive_amount,
)


def _connect(db_path: Path, label: str) -> sqlite3.Connection:
    if not db_path.is_file():
        raise NotFoundError(f"{label} database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class MockMailSource(MailSource):
    """The seeded MailDesk mailbox and its shared file tree."""

    def __init__(self, db_path: str | Path, shared_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.shared_root = Path(shared_root)

    @property
    def document_root(self) -> Path:
        return self.shared_root

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.db_path, "mailbox")

    def _message_row(
        self, conn: sqlite3.Connection, message_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no message '{message_id}' in the mailbox")
        return row

    def _attachment(self, conn: sqlite3.Connection, message_id: str, attachment_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM attachments WHERE id = ? AND message_id = ?",
            (attachment_id, message_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"no attachment '{attachment_id}' on message '{message_id}'"
            )
        return {
            "id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "path": row["path"],
        }

    def list_messages(self, folder: str = "inbox") -> list[MailSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT messages.*, COUNT(attachments.id) AS attachment_count
                FROM messages
                LEFT JOIN attachments ON attachments.message_id = messages.id
                WHERE messages.folder = ?
                GROUP BY messages.id
                ORDER BY messages.received_at DESC, messages.id DESC
                """,
                (folder,),
            ).fetchall()
        return [
            MailSummary(
                id=row["id"],
                folder=row["folder"],
                sender=row["sender_name"],
                sender_email=row["sender_email"],
                subject=row["subject"],
                received_at=row["received_at"],
                attachment_count=row["attachment_count"],
                unread=row["read_at"] is None,
                scenario=row["scenario"],
            )
            for row in rows
        ]

    def read_message(self, message_id: str) -> MailMessage:
        with closing(self._connect()) as conn:
            row = self._message_row(conn, message_id)
            rows = conn.execute(
                "SELECT * FROM attachments WHERE message_id = ? ORDER BY id",
                (message_id,),
            ).fetchall()
        attachments = tuple(
            MailAttachment(
                id=item["id"],
                filename=item["filename"],
                content_type=item["content_type"],
                size_bytes=item["size_bytes"],
                path=item["path"],
            )
            for item in rows
        )
        return MailMessage(
            id=row["id"],
            folder=row["folder"],
            sender=row["sender_name"],
            sender_email=row["sender_email"],
            subject=row["subject"],
            body=row["body"],
            received_at=row["received_at"],
            attachments=attachments,
            scenario=row["scenario"],
        )

    def read_attachment(self, message_id: str, attachment_id: str) -> bytes:
        with closing(self._connect()) as conn:
            attachment = self._attachment(conn, message_id, attachment_id)
        path = self.shared_root / attachment["path"]
        if not path.is_file():
            raise NotFoundError(f"attachment file not found: {attachment['path']}")
        return path.read_bytes()

    def mark_read(self, message_id: str) -> str:
        with closing(self._connect()) as conn, conn:
            row = self._message_row(conn, message_id)
            read_at = row["read_at"] or datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE messages SET read_at = ? WHERE id = ?", (read_at, message_id)
            )
        return read_at


class MockAccountingSystem(AccountingSystem):
    """The seeded LedgerLite database, read and written through the interface."""

    display_name = "LedgerLite"
    policy_system = "ledgerlite"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.db_path, "LedgerLite")

    def _one(
        self, conn: sqlite3.Connection, table: str, row_id: str, kind: str
    ) -> dict[str, Any]:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no {kind} '{row_id}' in LedgerLite")
        return dict(row)

    def _many(
        self, conn: sqlite3.Connection, table: str, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [dict(row) for row in conn.execute(sql, params)]

    def list_vendors(self, *, status: str | None = None) -> list[Vendor]:
        with closing(self._connect()) as conn:
            rows = self._many(conn, "vendors", {"status": status})
        return [_vendor(row) for row in rows]

    def get_vendor(self, vendor_id: str) -> Vendor:
        with closing(self._connect()) as conn:
            row = self._one(conn, "vendors", vendor_id, "vendor")
        return _vendor(row)

    def find_vendors(
        self, *, name: str | None = None, tax_id: str | None = None
    ) -> list[Vendor]:
        clauses = []
        params: list[Any] = []
        if tax_id is not None:
            clauses.append("tax_id = ?")
            params.append(tax_id)
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        sql = "SELECT * FROM vendors"
        if clauses:
            sql += " WHERE " + " OR ".join(clauses)
        sql += " ORDER BY id"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_vendor(dict(row)) for row in rows]

    def list_purchase_orders(
        self, *, vendor_id: str | None = None, status: str | None = None
    ) -> list[PurchaseOrder]:
        with closing(self._connect()) as conn:
            rows = self._many(
                conn, "purchase_orders", {"vendor_id": vendor_id, "status": status}
            )
        return [_purchase_order(row) for row in rows]

    def get_purchase_order(self, po_id: str) -> PurchaseOrder:
        with closing(self._connect()) as conn:
            row = self._one(conn, "purchase_orders", po_id, "purchase order")
        return _purchase_order(row)

    def get_goods_receipt(self, gr_id: str) -> GoodsReceipt:
        with closing(self._connect()) as conn:
            row = self._one(conn, "goods_receipts", gr_id, "goods receipt")
        return _goods_receipt(row)

    def list_bills(
        self,
        *,
        number: str | None = None,
        vendor_id: str | None = None,
        status: str | None = None,
    ) -> list[Bill]:
        with closing(self._connect()) as conn:
            rows = self._many(
                conn,
                "invoices",
                {"number": number, "vendor_id": vendor_id, "status": status},
            )
        return [_bill(row) for row in rows]

    def get_bill(self, bill_id: str) -> Bill:
        with closing(self._connect()) as conn:
            row = self._one(conn, "invoices", bill_id, "invoice")
        return _bill(row)

    def list_payments(self, bill_id: str) -> list[Payment]:
        with closing(self._connect()) as conn:
            rows = self._many(conn, "payments", {"invoice_id": bill_id})
        return [_payment(row) for row in rows]

    def find_pending_approval(self, bill_id: str) -> Approval | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM approvals
                WHERE subject_type = 'invoice' AND subject_id = ? AND status = 'pending'
                ORDER BY id LIMIT 1
                """,
                (bill_id,),
            ).fetchone()
        return _approval(dict(row)) if row is not None else None

    def create_purchase_bill(
        self,
        *,
        number: str,
        vendor_id: str,
        amount_cents: int,
        currency: str = "USD",
        po_id: str | None = None,
        gr_id: str | None = None,
    ) -> Bill:
        require_positive_amount(amount_cents)
        po_id = po_id or None
        gr_id = gr_id or None
        with closing(self._connect()) as conn, conn:
            self._one(conn, "vendors", vendor_id, "vendor")
            existing = conn.execute(
                "SELECT id, status FROM invoices WHERE vendor_id = ? AND number = ?",
                (vendor_id, number),
            ).fetchone()
            if existing is not None:
                raise duplicate_bill_error(
                    number=number,
                    vendor_id=vendor_id,
                    existing_id=existing["id"],
                    existing_status=existing["status"],
                )
            if po_id:
                order = self._one(conn, "purchase_orders", po_id, "purchase order")
                if order["amount_cents"] != amount_cents:
                    raise bill_reference_mismatch(
                        kind="purchase_order",
                        number=number,
                        vendor_id=vendor_id,
                        reference_id=po_id,
                        invoice_amount_cents=amount_cents,
                        expected_amount_cents=order["amount_cents"],
                        currency=currency,
                    )
            if gr_id:
                receipt = self._one(conn, "goods_receipts", gr_id, "goods receipt")
                if receipt["amount_cents"] != amount_cents:
                    raise bill_reference_mismatch(
                        kind="goods_receipt",
                        number=number,
                        vendor_id=vendor_id,
                        reference_id=gr_id,
                        invoice_amount_cents=amount_cents,
                        expected_amount_cents=receipt["amount_cents"],
                        currency=currency,
                    )
            failure = self._consume_failure(conn, f"invoice.create:{number}")
            if failure is not None:
                raise TransientError(failure)
            bill_id = _next_id(conn, "invoices", "INV")
            received_at = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO invoices
                    (id, number, vendor_id, po_id, gr_id, amount_cents, currency, status,
                     received_at, scenario)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, 'filed')
                """,
                (
                    bill_id,
                    number,
                    vendor_id,
                    po_id,
                    gr_id,
                    amount_cents,
                    currency,
                    received_at,
                ),
            )
        return Bill(
            id=bill_id,
            number=number,
            vendor_id=vendor_id,
            po_id=po_id,
            gr_id=gr_id,
            amount_cents=amount_cents,
            currency=currency,
            status="received",
            received_at=received_at,
            scenario="filed",
        )

    def create_vendor(
        self, *, name: str, tax_id: str, email: str, address: str
    ) -> Vendor:
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT id, status, tax_id FROM vendors WHERE tax_id = ? OR name = ? ORDER BY id",
                (tax_id, name),
            ).fetchall()
            if existing:
                match = next(
                    (row for row in existing if row["tax_id"] == tax_id), existing[0]
                )
                matched_on = "tax id" if match["tax_id"] == tax_id else "name"
                raise duplicate_vendor_error(
                    name=name,
                    tax_id=tax_id,
                    matched_on=matched_on,
                    existing_id=match["id"],
                    existing_status=match["status"],
                )
            vendor_id = _next_id(conn, "vendors", "V")
            created_at = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO vendors (id, name, tax_id, email, address, status, created_at, scenario)
                VALUES (?, ?, ?, ?, ?, 'active', ?, 'filed')
                """,
                (vendor_id, name, tax_id, email, address, created_at),
            )
        return Vendor(
            id=vendor_id,
            name=name,
            tax_id=tax_id,
            email=email,
            address=address,
            status="active",
            created_at=created_at,
            scenario="filed",
        )

    def schedule_payment(
        self,
        *,
        bill_id: str,
        amount_cents: int | None = None,
        scheduled_for: str | None = None,
    ) -> Payment:
        with closing(self._connect()) as conn, conn:
            bill = self._one(conn, "invoices", bill_id, "invoice")
            paid = self._full_amount(bill, amount_cents)
            payment_id = _next_id(conn, "payments", "PAY")
            due = scheduled_for or (
                datetime.now(UTC).date() + timedelta(days=30)
            ).isoformat()
            conn.execute(
                """
                INSERT INTO payments
                    (id, invoice_id, amount_cents, currency, scheduled_for, status, scenario)
                VALUES (?, ?, ?, ?, ?, 'scheduled', 'filed')
                """,
                (payment_id, bill_id, paid, bill["currency"], due),
            )
        return Payment(
            id=payment_id,
            bill_id=bill_id,
            amount_cents=paid,
            currency=bill["currency"],
            scheduled_for=due,
            status="scheduled",
        )

    def _full_amount(self, bill: dict[str, Any], amount_cents: int | None) -> int:
        if amount_cents is None:
            return int(bill["amount_cents"])
        if amount_cents != bill["amount_cents"]:
            raise partial_payment_error(
                bill=_bill(bill), amount_cents=amount_cents
            )
        return amount_cents

    @staticmethod
    def _consume_failure(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT remaining, message FROM failure_flags WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row["remaining"] <= 0:
            return None
        conn.execute(
            "UPDATE failure_flags SET remaining = remaining - 1 WHERE key = ?", (key,)
        )
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


def _vendor(row: dict[str, Any]) -> Vendor:
    return Vendor(
        id=row["id"],
        name=row["name"],
        tax_id=row["tax_id"],
        email=row["email"],
        address=row["address"],
        status=row["status"],
        created_at=row["created_at"],
        scenario=row["scenario"],
    )


def _purchase_order(row: dict[str, Any]) -> PurchaseOrder:
    return PurchaseOrder(
        id=row["id"],
        vendor_id=row["vendor_id"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=row["status"],
        issued_at=row["issued_at"],
        scenario=row["scenario"],
    )


def _goods_receipt(row: dict[str, Any]) -> GoodsReceipt:
    return GoodsReceipt(
        id=row["id"],
        po_id=row["po_id"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=row["status"],
        received_at=row["received_at"],
        scenario=row["scenario"],
    )


def _bill(row: dict[str, Any]) -> Bill:
    return Bill(
        id=row["id"],
        number=row["number"],
        vendor_id=row["vendor_id"],
        po_id=row["po_id"],
        gr_id=row["gr_id"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=row["status"],
        received_at=row["received_at"],
        scenario=row["scenario"],
    )


def _payment(row: dict[str, Any]) -> Payment:
    return Payment(
        id=row["id"],
        bill_id=row["invoice_id"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        scheduled_for=row["scheduled_for"],
        status=row["status"],
    )


def _approval(row: dict[str, Any]) -> Approval:
    return Approval(
        id=row["id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        kind=row["kind"],
        status=row["status"],
        requested_at=row["requested_at"],
        note=row["note"],
    )
