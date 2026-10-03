"""Verify: check a Run's outcome against ground truth, independently of the doer.

The Verifier reads the Task Pack's verification contract and inspects the real
LedgerLite database and the shared file tree. It never reads the executor's
messages and never trusts screenshots. Every check returns pass/fail with
evidence references; the Run completes only when every criterion passes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from company_operator.context.task_pack import TaskPack
from company_operator.engine.models import CheckResult
from company_operator.engine.states import RunState
from company_operator.runs.models import JournalEntry, Run
from company_operator.runs.store import RunStore

FILE_FIELD = re.compile(r"^Vendor:\s*(.+)$", re.MULTILINE)
NUMBER_FIELD = re.compile(r"^Invoice number:\s*(\S+)$", re.MULTILINE)
AMOUNT_FIELD = re.compile(r"^Amount due:\s*([\d,]+\.\d{2})\s*([A-Z]{3})$", re.MULTILINE)


@dataclass(frozen=True)
class CheckOutcome:
    """One check's verdict: pass/fail, a human detail, and evidence references."""

    ok: bool
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationContext:
    """Everything a check may read: the Run record and ground-truth locations."""

    run: Run
    store: RunStore
    erp_db_path: Path
    shared_root: Path


CheckFunction = Callable[[dict[str, Any], VerificationContext], CheckOutcome]


def _claims(context: VerificationContext, action: str) -> list[JournalEntry]:
    """Completed actions of one tool: the Run's own record of what it did."""
    return [
        entry
        for entry in context.store.list_journal(context.run.id)
        if entry.action == action and entry.status == "done"
    ]


def _invoice_for_claim(
    context: VerificationContext, entry: JournalEntry
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the LedgerLite row a filing claim names, or explain why it cannot."""
    payload = entry.payload or {}
    number = payload.get("number")
    vendor_id = payload.get("vendor_id")
    if not isinstance(number, str) or not isinstance(vendor_id, str):
        return None, f"{entry.key}: the filing action names no invoice number or vendor"
    rows = _erp_rows(
        context,
        "SELECT * FROM invoices WHERE number = ? AND vendor_id = ?",
        (number, vendor_id),
    )
    if len(rows) != 1:
        return None, (
            f"LedgerLite has {len(rows)} invoice(s) numbered {number} for {vendor_id}, expected one"
        )
    return rows[0], None


def _erp_rows(context: VerificationContext, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    if not context.erp_db_path.is_file():
        return []
    with closing(sqlite3.connect(context.erp_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


def _find_source(context: VerificationContext, number: str) -> Path | None:
    wanted = f"{number}.pdf"
    matches = sorted(
        path for path in context.shared_root.rglob("*.pdf") if path.name == wanted
    )
    return matches[0] if matches else None


def _parse_source(path: Path) -> dict[str, Any]:
    """Read the invoice fields out of the source PDF, independent of any tool."""
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    vendor = FILE_FIELD.search(text)
    number = NUMBER_FIELD.search(text)
    amount = AMOUNT_FIELD.search(text)
    if vendor is None or number is None or amount is None:
        raise ValueError("the source document has no readable invoice fields")
    try:
        cents = int((Decimal(amount.group(1).replace(",", "")) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse the amount {amount.group(1)!r}") from exc
    return {
        "vendor": vendor.group(1).strip(),
        "number": number.group(1),
        "amount_cents": cents,
        "currency": amount.group(2),
    }


def check_erp_invoice_matches(
    params: dict[str, Any], context: VerificationContext
) -> CheckOutcome:
    """Each filed invoice agrees with its source document and LedgerLite records."""
    match_fields = params.get("match") or ["vendor_id", "invoice_number", "amount_cents"]
    claims = _claims(context, "erp.file_invoice")
    if not claims:
        return CheckOutcome(False, "no successful erp.file_invoice action is recorded on this Run")
    failures: list[str] = []
    evidence: list[str] = []
    for entry in claims:
        invoice, problem = _invoice_for_claim(context, entry)
        if problem is not None:
            failures.append(problem)
            continue
        if invoice["status"] not in ("received", "approved", "paid"):
            failures.append(f"{invoice['id']}: status '{invoice['status']}' is not a live invoice")
            continue
        source = _find_source(context, invoice["number"])
        if source is None:
            failures.append(f"no source document found for invoice {invoice['number']}")
            continue
        try:
            parsed = _parse_source(source)
        except Exception as exc:  # noqa: BLE001 - a broken source is a failed check, not a crash
            failures.append(f"cannot read source document {source.name}: {exc}")
            continue
        reference = source.relative_to(context.shared_root).as_posix()
        if "invoice_number" in match_fields and parsed["number"] != invoice["number"]:
            failures.append(
                f"{invoice['id']}: LedgerLite number {invoice['number']} != source {parsed['number']}"
            )
        if "amount_cents" in match_fields and parsed["amount_cents"] != invoice["amount_cents"]:
            failures.append(
                f"{invoice['id']}: LedgerLite amount {invoice['amount_cents'] / 100:,.2f} "
                f"!= source amount {parsed['amount_cents'] / 100:,.2f}"
            )
        if "vendor_id" in match_fields:
            vendors = _erp_rows(context, "SELECT * FROM vendors WHERE name = ?", (parsed["vendor"],))
            if len(vendors) != 1 or vendors[0]["id"] != invoice["vendor_id"]:
                failures.append(
                    f"{invoice['id']}: source vendor {parsed['vendor']!r} does not resolve to "
                    f"{invoice['vendor_id']}"
                )
        failures.extend(_reference_failures(context, invoice))
        if not any(failure.startswith(invoice["id"]) for failure in failures):
            evidence.extend([f"erp:{invoice['id']}", f"source:{reference}"])
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(claims)} filed invoice(s) agree with their source documents and LedgerLite records",
        tuple(evidence),
    )


def _reference_failures(context: VerificationContext, invoice: dict[str, Any]) -> list[str]:
    """A filed invoice must agree with its purchase order and goods receipt."""
    failures: list[str] = []
    if invoice["po_id"]:
        orders = _erp_rows(context, "SELECT * FROM purchase_orders WHERE id = ?", (invoice["po_id"],))
        if len(orders) != 1:
            failures.append(f"{invoice['id']}: purchase order {invoice['po_id']} is missing")
        elif orders[0]["vendor_id"] != invoice["vendor_id"]:
            failures.append(
                f"{invoice['id']}: purchase order {invoice['po_id']} belongs to another vendor"
            )
        elif orders[0]["amount_cents"] != invoice["amount_cents"]:
            failures.append(
                f"{invoice['id']}: amount {invoice['amount_cents'] / 100:,.2f} does not match "
                f"purchase order {invoice['po_id']}"
            )
    if invoice["gr_id"]:
        receipts = _erp_rows(
            context, "SELECT * FROM goods_receipts WHERE id = ?", (invoice["gr_id"],)
        )
        if len(receipts) != 1:
            failures.append(f"{invoice['id']}: goods receipt {invoice['gr_id']} is missing")
        elif invoice["po_id"] and receipts[0]["po_id"] != invoice["po_id"]:
            failures.append(
                f"{invoice['id']}: goods receipt {invoice['gr_id']} belongs to another purchase order"
            )
        elif receipts[0]["amount_cents"] != invoice["amount_cents"]:
            failures.append(
                f"{invoice['id']}: amount {invoice['amount_cents'] / 100:,.2f} does not match "
                f"goods receipt {invoice['gr_id']}"
            )
    return failures


def check_erp_payment_state(params: dict[str, Any], context: VerificationContext) -> CheckOutcome:
    """Every filed invoice is paid, scheduled, or (when allowed) awaiting approval."""
    allow_pending = bool(params.get("allow_pending_approval", False))
    claims = _claims(context, "erp.file_invoice")
    if not claims:
        return CheckOutcome(False, "no successful erp.file_invoice action is recorded on this Run")
    failures: list[str] = []
    evidence: list[str] = []
    for entry in claims:
        invoice, problem = _invoice_for_claim(context, entry)
        if problem is not None:
            failures.append(problem)
            continue
        invoice_id = invoice["id"]
        payments = _erp_rows(
            context,
            "SELECT * FROM payments WHERE invoice_id = ? AND status IN ('scheduled', 'paid')",
            (invoice_id,),
        )
        covering = [
            payment for payment in payments if payment["amount_cents"] == invoice["amount_cents"]
        ]
        if covering:
            evidence.append(f"payment:{covering[0]['id']}:{covering[0]['status']}")
            continue
        if payments:
            failures.append(
                f"invoice {invoice_id}: payment {payments[0]['id']} covers "
                f"{payments[0]['amount_cents'] / 100:,.2f} of "
                f"{invoice['amount_cents'] / 100:,.2f}"
            )
            continue
        if allow_pending:
            approvals = _erp_rows(
                context,
                "SELECT * FROM approvals WHERE subject_type = 'invoice' AND subject_id = ? "
                "AND status = 'pending'",
                (invoice_id,),
            )
            if approvals:
                evidence.append(f"approval:{approvals[0]['id']}:pending")
                continue
        expected = "a scheduled payment or a pending approval" if allow_pending else "a scheduled payment"
        failures.append(f"invoice {invoice_id} has neither {expected}")
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(True, f"{len(claims)} filed invoice(s) have a payment or a pending approval", tuple(evidence))


def check_file_archived(params: dict[str, Any], context: VerificationContext) -> CheckOutcome:
    """Every archive action landed its source document in the configured directory."""
    directory = params.get("directory", "archive")
    claims = _claims(context, "files.archive")
    if not claims:
        return CheckOutcome(
            False,
            f"no successful files.archive action is recorded on this Run; "
            f"expected source documents in '{directory}'",
        )
    failures: list[str] = []
    evidence: list[str] = []
    for entry in claims:
        payload = entry.payload or {}
        source = payload.get("path")
        if not isinstance(source, str) or not source:
            failures.append(f"{entry.key}: the archive action names no source document")
            continue
        archived_into = payload.get("directory") or "archive"
        if archived_into != directory:
            failures.append(
                f"'{source}' was archived into '{archived_into}', not '{directory}'"
            )
            continue
        origin = context.shared_root / source
        if origin.is_file():
            failures.append(f"'{source}' is still at its original location, not archived")
            continue
        target = context.shared_root / directory / Path(source).name
        if not target.is_file():
            failures.append(f"'{source}' is not present in '{directory}'")
            continue
        evidence.append(f"file:{target.relative_to(context.shared_root).as_posix()}")
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(True, f"{len(claims)} source document(s) archived in '{directory}'", tuple(evidence))


CHECKS: dict[str, CheckFunction] = {
    "erp_invoice_matches": check_erp_invoice_matches,
    "erp_payment_state": check_erp_payment_state,
    "file_archived": check_file_archived,
}


def verify_run(
    run_id: str,
    task_pack: TaskPack,
    store: RunStore,
    *,
    erp_db_path: str | Path,
    shared_root: str | Path,
) -> Run:
    """Run every verification check and move the Run to its verified outcome."""
    run = store.get_run(run_id)
    if run.state is not RunState.VERIFYING:
        return run
    context = VerificationContext(
        run=run,
        store=store,
        erp_db_path=Path(erp_db_path),
        shared_root=Path(shared_root),
    )
    results: list[CheckResult] = []
    for check in task_pack.verification:
        function = CHECKS.get(check.check)
        if function is None:
            outcome = CheckOutcome(False, f"unknown check function '{check.check}'")
        else:
            try:
                outcome = function(check.params, context)
            except Exception as exc:  # noqa: BLE001 - a broken check is a failed check, not a crash
                outcome = CheckOutcome(False, f"check crashed: {type(exc).__name__}: {exc}")
        results.append(
            CheckResult(
                id=check.id,
                description=check.description,
                ok=outcome.ok,
                detail=outcome.detail,
                evidence=list(outcome.evidence),
            )
        )
    store.save_verification(run_id, results)
    failed = [result.id for result in results if not result.ok]
    if failed:
        store.set_error(run_id, f"verification failed: {', '.join(failed)}")
        return store.transition(run_id, RunState.FAILED)
    return store.transition(run_id, RunState.COMPLETED)
