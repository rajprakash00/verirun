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
from contextlib import closing, suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from verirun.context.task_pack import TaskPack
from verirun.engine.models import CheckResult
from verirun.engine.states import RunState
from verirun.llm.client import LLMClient
from verirun.runs.models import JournalEntry, Run
from verirun.runs.store import RunStore
from verirun.tools.vision import read_scanned_invoice

FILE_FIELD = re.compile(r"^Vendor:\s*(.+)$", re.MULTILINE)
NUMBER_FIELD = re.compile(r"^Invoice number:\s*(\S+)$", re.MULTILINE)
AMOUNT_FIELD = re.compile(r"^Amount due:\s*([\d,]+\.\d{2})\s*([A-Z]{3})$", re.MULTILINE)

TAX_FORM_MARKER = "Taxpayer Identification Number"
TAX_FORM_NAME = re.compile(r"^Name:\s*(.+)$", re.MULTILINE)
TAX_FORM_TAX_ID = re.compile(r"^Taxpayer Identification Number:\s*(\S+)\s*$", re.MULTILINE)
TAX_FORM_ADDRESS = re.compile(r"^Address:\s*(.+)$", re.MULTILINE)
TAX_FORM_EMAIL = re.compile(r"^Email:\s*(\S+)\s*$", re.MULTILINE)


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
    confidence_threshold: float | None = None
    client: LLMClient | None = None


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


def _pdf_text(path: Path) -> str:
    """The text layer of a PDF, joined across pages."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _parse_source(path: Path) -> dict[str, Any]:
    """Read the invoice fields out of the source PDF, independent of any tool."""
    text = _pdf_text(path)
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


def _verifier_scan(
    context: VerificationContext, source: Path
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """Read a scanned source with the Verifier's own vision call.

    A scan has no text layer, so the Verifier renders the file and asks the
    vision model itself. It never reads the executor's extraction; it checks the
    source document, and only trusts its own read when every confidence clears
    the Task Pack's threshold. Returns (parsed fields, lowest confidence, problem).
    """
    if context.client is None:
        return None, None, "the Verifier has no vision model client to read the scan"
    if context.confidence_threshold is None:
        return None, None, "the Task Pack sets no extraction confidence threshold"
    return read_scanned_invoice(context.client, source, context.confidence_threshold)


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
        reference = source.relative_to(context.shared_root).as_posix()
        extraction_evidence: str | None = None
        try:
            parsed = _parse_source(source)
        except Exception as exc:  # noqa: BLE001 - a broken source is a failed check, not a crash
            parsed, minimum, problem = _verifier_scan(context, source)
            if problem is not None:
                failures.append(
                    f"cannot read source document {source.name}: {exc}; {problem}"
                )
                continue
            assert minimum is not None
            extraction_evidence = f"extraction:{reference}@{minimum:.2f}"
        assert parsed is not None
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
            if extraction_evidence is not None:
                evidence.append(extraction_evidence)
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


def _vendor_for_claim(
    context: VerificationContext, entry: JournalEntry
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the LedgerLite row a vendor creation claim names."""
    data = (entry.result or {}).get("data") or {}
    vendor_id = data.get("vendor_id")
    if not isinstance(vendor_id, str) or not vendor_id:
        return None, f"{entry.key}: the creation action names no vendor id"
    rows = _erp_rows(context, "SELECT * FROM vendors WHERE id = ?", (vendor_id,))
    if len(rows) != 1:
        return None, f"LedgerLite has {len(rows)} vendor(s) with id {vendor_id}, expected one"
    return rows[0], None


def _parse_tax_form(path: Path) -> dict[str, str]:
    """Read the vendor fields out of a tax form PDF, independent of any tool."""
    text = _pdf_text(path)
    name = TAX_FORM_NAME.search(text)
    tax_id = TAX_FORM_TAX_ID.search(text)
    address = TAX_FORM_ADDRESS.search(text)
    email = TAX_FORM_EMAIL.search(text)
    if name is None or tax_id is None or address is None or email is None:
        raise ValueError("the tax form has no readable vendor fields")
    return {
        "name": name.group(1).strip(),
        "tax_id": tax_id.group(1),
        "address": address.group(1).strip(),
        "email": email.group(1),
    }


def _find_tax_forms(
    context: VerificationContext, tax_id: str
) -> list[tuple[Path, dict[str, str] | None]]:
    """Every tax form in the shared tree that carries this tax id.

    Copies are allowed; a form that cannot be parsed is returned with ``None``
    so the check can report it instead of silently ignoring it.
    """
    matches: list[tuple[Path, dict[str, str] | None]] = []
    for path in sorted(context.shared_root.rglob("*.pdf")):
        text = ""
        with suppress(Exception):  # an unreadable PDF is simply not a match
            text = _pdf_text(path)
        if tax_id not in text or TAX_FORM_MARKER not in text:
            continue
        parsed: dict[str, str] | None = None
        with suppress(Exception):
            parsed = _parse_tax_form(path)
        matches.append((path, parsed))
    return matches


def check_erp_vendor_matches(
    params: dict[str, Any], context: VerificationContext
) -> CheckOutcome:
    """Every created vendor exists exactly once and agrees with its tax form."""
    match_fields = params.get("match") or ["name", "tax_id", "email", "address"]
    claims = _claims(context, "erp.create_vendor")
    if not claims:
        return CheckOutcome(False, "no successful erp.create_vendor action is recorded on this Run")
    failures: list[str] = []
    evidence: list[str] = []
    for entry in claims:
        vendor, problem = _vendor_for_claim(context, entry)
        if problem is not None:
            failures.append(problem)
            continue
        vendor_id = vendor["id"]
        duplicates = _erp_rows(
            context,
            "SELECT * FROM vendors WHERE tax_id = ? OR name = ?",
            (vendor["tax_id"], vendor["name"]),
        )
        if len(duplicates) != 1 or duplicates[0]["id"] != vendor_id:
            failures.append(
                f"{vendor_id}: tax id {vendor['tax_id']} or name {vendor['name']!r} "
                f"appears on {len(duplicates)} vendors, expected exactly one"
            )
            continue
        sources = _find_tax_forms(context, vendor["tax_id"])
        if not sources:
            failures.append(
                f"{vendor_id}: no tax form with tax id {vendor['tax_id']} was found"
            )
            continue
        unreadable = [path.name for path, parsed in sources if parsed is None]
        if unreadable:
            failures.append(
                f"{vendor_id}: cannot read tax form(s): {', '.join(unreadable)}"
            )
            continue
        reads = {
            tuple(parsed.get(field) for field in match_fields) for _, parsed in sources
        }
        if len(reads) > 1:
            failures.append(
                f"{vendor_id}: {len(sources)} tax forms with tax id "
                f"{vendor['tax_id']} disagree on the vendor fields"
            )
            continue
        source, parsed = sources[0]
        reference = source.relative_to(context.shared_root).as_posix()
        for field in match_fields:
            if parsed.get(field) != vendor.get(field):
                failures.append(
                    f"{vendor_id}: {field} {vendor.get(field)!r} does not match "
                    f"the tax form {parsed.get(field)!r}"
                )
        if not any(failure.startswith(vendor_id) for failure in failures):
            evidence.extend([f"erp:{vendor_id}", f"source:{reference}"])
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(claims)} created vendor(s) agree with their tax forms and LedgerLite records",
        tuple(evidence),
    )


CHECKS: dict[str, CheckFunction] = {
    "erp_invoice_matches": check_erp_invoice_matches,
    "erp_payment_state": check_erp_payment_state,
    "erp_vendor_matches": check_erp_vendor_matches,
    "file_archived": check_file_archived,
}


def verify_run(
    run_id: str,
    task_pack: TaskPack,
    store: RunStore,
    *,
    erp_db_path: str | Path,
    shared_root: str | Path,
    client: LLMClient | None = None,
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
        confidence_threshold=(
            task_pack.extraction.confidence_threshold if task_pack.extraction else None
        ),
        client=client,
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
