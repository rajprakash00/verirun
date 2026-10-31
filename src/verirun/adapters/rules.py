"""Rejection payloads and amount checks shared by the accounting adapters.

Each adapter enforces its rules against its own storage, but the error message
and ``data`` payload must be identical: the engine's failure policy and the
evidence read the same comparison detail from the Mock Suite and the in-memory
fake. The parameterized contract tests in ``tests/test_adapters.py`` guard the
rest.
"""

from __future__ import annotations

from verirun.adapters.base import Bill, ConflictError


def money(cents: int, currency: str) -> str:
    return f"{cents / 100:,.2f} {currency}"


def require_positive_amount(amount_cents: int) -> None:
    if amount_cents <= 0:
        raise ConflictError(
            f"amount must be positive, got {amount_cents} cents",
            data={"invalid_amount_cents": amount_cents},
        )


def duplicate_bill_error(
    *, number: str, vendor_id: str, existing_id: str, existing_status: str
) -> ConflictError:
    return ConflictError(
        f"Invoice {number} already exists for {vendor_id} as {existing_id}",
        data={
            "duplicate": True,
            "number": number,
            "vendor_id": vendor_id,
            "existing_invoice_id": existing_id,
            "existing_status": existing_status,
        },
    )


def bill_reference_mismatch(
    *,
    kind: str,
    number: str,
    vendor_id: str,
    reference_id: str,
    invoice_amount_cents: int,
    expected_amount_cents: int,
    currency: str,
) -> ConflictError:
    label = "purchase order" if kind == "purchase_order" else "goods receipt"
    key = "po_id" if kind == "purchase_order" else "gr_id"
    return ConflictError(
        (
            f"Invoice {number} is {money(invoice_amount_cents, currency)} but "
            f"{label} {reference_id} is {money(expected_amount_cents, currency)}"
        ),
        data={
            "mismatch": kind,
            "number": number,
            "vendor_id": vendor_id,
            key: reference_id,
            "invoice_amount_cents": invoice_amount_cents,
            "expected_amount_cents": expected_amount_cents,
            "currency": currency,
        },
    )


def duplicate_vendor_error(
    *,
    name: str,
    tax_id: str,
    matched_on: str,
    existing_id: str,
    existing_status: str,
) -> ConflictError:
    return ConflictError(
        f"A vendor with the same {matched_on} already exists: {existing_id}",
        data={
            "duplicate": True,
            "existing_vendor_id": existing_id,
            "existing_status": existing_status,
            "name": name,
            "tax_id": tax_id,
        },
    )


def partial_payment_error(
    *, bill: Bill, amount_cents: int
) -> ConflictError:
    return ConflictError(
        f"payment must cover the full invoice {bill.id} "
        f"({money(bill.amount_cents, bill.currency)}); splitting is forbidden",
        data={
            "mismatch": "payment_amount",
            "invoice_id": bill.id,
            "payment_amount_cents": amount_cents,
            "expected_amount_cents": bill.amount_cents,
            "currency": bill.currency,
        },
    )
