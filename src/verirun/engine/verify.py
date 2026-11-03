"""Verify: check a Run's outcome against ground truth, independently of the doer.

The Verifier reads the Task Pack's verification contract, the read-only
accounting adapter, and the shared file tree. It never reads the executor's
messages and never trusts screenshots. Every check returns pass/fail with
evidence references; the Run completes only when every criterion passes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from verirun.adapters import (
    AccountingReads,
    Approval,
    Bill,
    GoodsReceipt,
    NotFoundError,
    Payment,
    PurchaseOrder,
    Vendor,
)
from verirun.adapters.matching import gstin_key, name_key, reference_key
from verirun.context.task_pack import INVOICE_EXTRACT_TOOL, TaskPack
from verirun.engine.models import CheckResult
from verirun.engine.states import RunState
from verirun.india.source import find_source_invoice
from verirun.india.validation import validate_gstin, validate_tax
from verirun.llm.client import LLMClient
from verirun.runs.models import JournalEntry, Run
from verirun.runs.store import RunStore
from verirun.tools.vision import read_scanned_invoice

LIVE_BILL_STATUSES = frozenset({"received", "approved", "paid"})
PAYMENT_STATUSES = frozenset({"scheduled", "paid"})

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
    """Everything a check may read: the Run record and ground-truth sources."""

    run: Run
    store: RunStore
    accounting: AccountingReads
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
) -> tuple[Bill | None, str | None]:
    """Resolve the system-of-record bill a filing claim names, or explain why it cannot."""
    payload = entry.payload or {}
    number = payload.get("number")
    vendor_id = payload.get("vendor_id")
    if not isinstance(number, str) or not isinstance(vendor_id, str):
        return None, f"{entry.key}: the filing action names no invoice number or vendor"
    bills = _bills(context, number=number, vendor_id=vendor_id)
    if len(bills) != 1:
        return None, (
            f"{context.accounting.display_name} has {len(bills)} invoice(s) numbered "
            f"{number} for {vendor_id}, expected one"
        )
    return bills[0], None


def _claimed_bill_id(
    context: VerificationContext, *, number: str, vendor_id: str
) -> str | None:
    """The bill id this Run's own completed write names, if any."""
    for entry in _claims(context, "erp.file_invoice"):
        payload = entry.payload or {}
        if payload.get("number") != number or payload.get("vendor_id") != vendor_id:
            continue
        data = (entry.result or {}).get("data") or {}
        invoice_id = data.get("invoice_id")
        if isinstance(invoice_id, str) and invoice_id:
            return invoice_id
    return None


def _bills(context: VerificationContext, **filters: str) -> list[Bill]:
    try:
        return context.accounting.list_bills(**filters)
    except NotFoundError:
        return []


def _vendors(context: VerificationContext, **filters: str) -> list[Vendor]:
    try:
        return context.accounting.find_vendors(**filters)
    except NotFoundError:
        return []


def _purchase_order(context: VerificationContext, po_id: str) -> PurchaseOrder | None:
    try:
        return context.accounting.get_purchase_order(po_id)
    except NotFoundError:
        return None


def _purchase_orders(context: VerificationContext, **filters: str) -> list[PurchaseOrder]:
    try:
        return context.accounting.list_purchase_orders(**filters)
    except NotFoundError:
        return []


def _goods_receipt(context: VerificationContext, gr_id: str) -> GoodsReceipt | None:
    try:
        return context.accounting.get_goods_receipt(gr_id)
    except NotFoundError:
        return None


def _payments(context: VerificationContext, bill_id: str) -> list[Payment]:
    try:
        return context.accounting.list_payments(bill_id)
    except NotFoundError:
        return []


def _pending_approval(context: VerificationContext, bill_id: str) -> Approval | None:
    try:
        return context.accounting.find_pending_approval(bill_id)
    except NotFoundError:
        return None


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
    """Each filed invoice agrees with its source document and system-of-record bill."""
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
        assert invoice is not None
        if invoice.status not in LIVE_BILL_STATUSES:
            failures.append(f"{invoice.id}: status '{invoice.status}' is not a live invoice")
            continue
        source = _find_source(context, invoice.number)
        if source is None:
            failures.append(f"no source document found for invoice {invoice.number}")
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
        if "invoice_number" in match_fields and parsed["number"] != invoice.number:
            failures.append(
                f"{invoice.id}: {context.accounting.display_name} number {invoice.number} "
                f"!= source {parsed['number']}"
            )
        if "amount_cents" in match_fields and parsed["amount_cents"] != invoice.amount_cents:
            failures.append(
                f"{invoice.id}: {context.accounting.display_name} amount "
                f"{invoice.amount_cents / 100:,.2f} "
                f"!= source amount {parsed['amount_cents'] / 100:,.2f}"
            )
        if "vendor_id" in match_fields:
            vendors = _vendors(context, name=parsed["vendor"])
            if len(vendors) != 1 or vendors[0].id != invoice.vendor_id:
                failures.append(
                    f"{invoice.id}: source vendor {parsed['vendor']!r} does not resolve to "
                    f"{invoice.vendor_id}"
                )
        failures.extend(_reference_failures(context, invoice))
        if not any(failure.startswith(invoice.id) for failure in failures):
            evidence.extend([f"erp:{invoice.id}", f"source:{reference}"])
            if extraction_evidence is not None:
                evidence.append(extraction_evidence)
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(claims)} filed invoice(s) agree with their source documents and "
        f"{context.accounting.display_name} records",
        tuple(evidence),
    )


def _reference_failures(context: VerificationContext, invoice: Bill) -> list[str]:
    """A filed invoice must agree with its purchase order and goods receipt."""
    failures: list[str] = []
    if invoice.po_id:
        order = _purchase_order(context, invoice.po_id)
        if order is None:
            failures.append(f"{invoice.id}: purchase order {invoice.po_id} is missing")
        elif order.vendor_id != invoice.vendor_id:
            failures.append(
                f"{invoice.id}: purchase order {invoice.po_id} belongs to another vendor"
            )
        elif order.amount_cents != invoice.amount_cents:
            failures.append(
                f"{invoice.id}: amount {invoice.amount_cents / 100:,.2f} does not match "
                f"purchase order {invoice.po_id}"
            )
    if invoice.gr_id:
        receipt = _goods_receipt(context, invoice.gr_id)
        if receipt is None:
            failures.append(f"{invoice.id}: goods receipt {invoice.gr_id} is missing")
        elif invoice.po_id and receipt.po_id != invoice.po_id:
            failures.append(
                f"{invoice.id}: goods receipt {invoice.gr_id} belongs to another purchase order"
            )
        elif receipt.amount_cents != invoice.amount_cents:
            failures.append(
                f"{invoice.id}: amount {invoice.amount_cents / 100:,.2f} does not match "
                f"goods receipt {invoice.gr_id}"
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
        assert invoice is not None
        invoice_id = invoice.id
        payments = [
            payment
            for payment in _payments(context, invoice_id)
            if payment.status in PAYMENT_STATUSES
        ]
        covering = [
            payment for payment in payments if payment.amount_cents == invoice.amount_cents
        ]
        if covering:
            evidence.append(f"payment:{covering[0].id}:{covering[0].status}")
            continue
        if payments:
            failures.append(
                f"invoice {invoice_id}: payment {payments[0].id} covers "
                f"{payments[0].amount_cents / 100:,.2f} of "
                f"{invoice.amount_cents / 100:,.2f}"
            )
            continue
        if allow_pending:
            approval = _pending_approval(context, invoice_id)
            if approval is not None:
                evidence.append(f"approval:{approval.id}:pending")
                continue
        expected = "a scheduled payment or a pending approval" if allow_pending else "a scheduled payment"
        failures.append(f"invoice {invoice_id} has neither {expected}")
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(True, f"{len(claims)} filed invoice(s) have a payment or a pending approval", tuple(evidence))


def check_accounting_invoice_matches(
    params: dict[str, Any], context: VerificationContext
) -> CheckOutcome:
    """Each extracted invoice matches the system of record before any bill exists.

    The read is the Verifier's own: the extraction names the supplier GSTIN,
    name, number, total, and PO reference, and the check resolves them through
    the read-only accounting client. It fails on an ambiguous vendor, a GSTIN or
    name that disagrees with the vendor master, an existing bill with the same
    number, or a PO whose amount or currency differs.
    """
    require_purchase_order = bool(params.get("require_purchase_order", False))
    raw_statuses = params.get("purchase_order_statuses", ["open"])
    if isinstance(raw_statuses, str):
        raw_statuses = [raw_statuses]
    purchase_order_statuses = {str(status).casefold() for status in raw_statuses}
    extractions = [
        record
        for record in context.store.list_observations(context.run.id)
        if record.tool == INVOICE_EXTRACT_TOOL and record.ok
    ]
    if not extractions:
        return CheckOutcome(
            False,
            f"no successful {INVOICE_EXTRACT_TOOL} action is recorded on this Run",
        )
    failures: list[str] = []
    evidence: list[str] = []
    for record in extractions:
        data = record.data or {}
        parsed = data.get("parsed")
        if not isinstance(parsed, dict):
            failures.append(f"observation {record.id}: the extraction has no parsed invoice")
            continue
        name = parsed.get("supplier_name")
        gstin = parsed.get("supplier_gstin")
        number = parsed.get("invoice_number")
        total = parsed.get("total_paise")
        currency = parsed.get("currency")
        if not all(isinstance(value, str) and value for value in (name, gstin, number, currency)):
            failures.append(
                f"observation {record.id}: the parsed invoice is missing supplier, "
                "GSTIN, number, or currency"
            )
            continue
        if not isinstance(total, int):
            failures.append(f"observation {record.id}: the parsed invoice has no total")
            continue
        before = len(failures)
        vendors = _vendors(context, name=name, tax_id=gstin)
        if len(vendors) != 1:
            failures.append(
                f"{number}: {context.accounting.display_name} has {len(vendors)} "
                f"vendor(s) matching GSTIN {gstin} or name {name!r}, expected one"
            )
            continue
        vendor = vendors[0]
        if gstin_key(vendor.tax_id) != gstin_key(gstin):
            failures.append(
                f"{number}: vendor {vendor.id} GSTIN {vendor.tax_id!r} does not match "
                f"the source GSTIN {gstin!r}"
            )
            continue
        if name_key(vendor.name) != name_key(name):
            failures.append(
                f"{number}: vendor {vendor.id} name {vendor.name!r} does not match "
                f"the source name {name!r}"
            )
            continue
        existing = [
            bill
            for bill in _bills(context, number=number, vendor_id=vendor.id)
            if bill.status in LIVE_BILL_STATUSES
        ]
        if existing:
            claimed = _claimed_bill_id(
                context, number=number, vendor_id=vendor.id
            )
            if len(existing) != 1 or existing[0].id != claimed:
                evidence.append(f"accounting:bill:{existing[0].id}")
                failures.append(
                    f"{number}: {context.accounting.display_name} already has a bill "
                    f"for vendor {vendor.id} as {existing[0].id}"
                )
                continue
            # This Run created the bill being verified; it is not a duplicate.
            evidence.append(f"accounting:bill:{existing[0].id}")
        purchase_order = _po_reference(data)
        if purchase_order is not None:
            orders = [
                order
                for order in _purchase_orders(context, vendor_id=vendor.id)
                if order.status.casefold() in purchase_order_statuses
                and (
                    reference_key(order.number or order.id)
                    == reference_key(purchase_order)
                    or reference_key(order.id) == reference_key(purchase_order)
                )
            ]
            if len(orders) != 1:
                failures.append(
                    f"{number}: {context.accounting.display_name} has {len(orders)} "
                    f"purchase order(s) {purchase_order!r} for vendor {vendor.id}, expected one"
                )
                continue
            order = orders[0]
            if order.amount_cents != total:
                failures.append(
                    f"{number}: amount {total / 100:,.2f} does not match purchase "
                    f"order {order.id} ({order.amount_cents / 100:,.2f})"
                )
                continue
            if order.currency.upper() != currency.upper():
                failures.append(
                    f"{number}: currency {currency} does not match purchase order "
                    f"{order.id} ({order.currency})"
                )
                continue
        elif require_purchase_order:
            failures.append(
                f"{number}: the extraction cites no purchase order and the Task Pack "
                "requires one"
            )
            continue
        if len(failures) == before:
            evidence.append(f"accounting:vendor:{vendor.id}")
            evidence.append(f"accounting:no-bill:{vendor.id}:{number}")
            if purchase_order is not None:
                evidence.append(f"accounting:purchaseorder:{order.id}")
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(extractions)} extracted invoice(s) match their vendor, duplicate, and "
        f"{context.accounting.display_name} purchase-order records",
        tuple(evidence),
    )


def check_india_bill_matches(
    params: dict[str, Any], context: VerificationContext
) -> CheckOutcome:
    """Each purchase bill exists exactly once and matches its source tax invoice.

    The source is parsed by the Verifier itself. The check covers the bill's
    existence, vendor identity and GSTIN, invoice number, amount, the GSTIN
    check digit and tax arithmetic, the purchase order when the bill cites one,
    and the source document reference.
    """
    claims = _claims(context, "erp.file_invoice")
    if not claims:
        return CheckOutcome(
            False, "no successful erp.file_invoice action is recorded on this Run"
        )
    failures: list[str] = []
    evidence: list[str] = []
    for entry in claims:
        invoice, problem = _invoice_for_claim(context, entry)
        if problem is not None:
            failures.append(problem)
            continue
        assert invoice is not None
        if invoice.status not in LIVE_BILL_STATUSES:
            failures.append(
                f"{invoice.id}: status '{invoice.status}' is not a live purchase bill"
            )
            continue
        source = find_source_invoice(context.shared_root, invoice.number)
        if source is None:
            failures.append(
                f"{invoice.id}: no source invoice for number {invoice.number} "
                "could be read in the shared tree"
            )
            continue
        path, parsed = source
        reference = path.relative_to(context.shared_root).as_posix()
        before = len(failures)
        if reference_key(parsed.invoice_number) != reference_key(invoice.number):
            failures.append(
                f"{invoice.id}: {context.accounting.display_name} number "
                f"{invoice.number} != source {parsed.invoice_number}"
            )
        if parsed.currency.upper() != invoice.currency.upper():
            failures.append(
                f"{invoice.id}: bill currency {invoice.currency} != source "
                f"{parsed.currency}"
            )
        if parsed.total_paise != invoice.amount_cents:
            failures.append(
                f"{invoice.id}: {context.accounting.display_name} amount "
                f"{invoice.amount_cents / 100:,.2f} != source total "
                f"{parsed.total_paise / 100:,.2f} {parsed.currency}"
            )
        try:
            vendor = context.accounting.get_vendor(invoice.vendor_id)
        except NotFoundError:
            failures.append(
                f"{invoice.id}: vendor {invoice.vendor_id} is missing from "
                f"{context.accounting.display_name}"
            )
            vendor = None
        if vendor is not None:
            if gstin_key(vendor.tax_id) != gstin_key(parsed.supplier_gstin):
                failures.append(
                    f"{invoice.id}: vendor GSTIN {vendor.tax_id!r} != source "
                    f"{parsed.supplier_gstin!r}"
                )
            if name_key(vendor.name) != name_key(parsed.supplier_name):
                failures.append(
                    f"{invoice.id}: vendor name {vendor.name!r} != source "
                    f"{parsed.supplier_name!r}"
                )
        gstin = validate_gstin(parsed.supplier_gstin)
        if not gstin.ok:
            failures.append(f"{invoice.id}: {gstin.detail}")
        taxes = validate_tax(
            taxable_paise=parsed.taxable_paise,
            cgst_paise=parsed.cgst_paise,
            sgst_paise=parsed.sgst_paise,
            igst_paise=parsed.igst_paise,
            total_paise=parsed.total_paise,
        )
        for tax in taxes:
            if not tax.ok:
                failures.append(f"{invoice.id}: {tax.detail}")
        failures.extend(_india_reference_failures(context, invoice))
        if len(failures) == before:
            evidence.extend(
                [
                    f"erp:{invoice.id}",
                    f"source:{reference}",
                    f"gstin:{parsed.supplier_gstin}",
                    "tax:tax_sum",
                    "tax:tax_split",
                ]
            )
            if invoice.po_id:
                evidence.append(f"accounting:purchaseorder:{invoice.po_id}")
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(claims)} purchase bill(s) exist exactly once and agree with their "
        "source tax invoice and GST validations",
        tuple(evidence),
    )


def _india_reference_failures(
    context: VerificationContext, invoice: Bill
) -> list[str]:
    """A Zoho purchase bill must agree with the purchase order it cites."""
    if not invoice.po_id:
        return []
    order = _purchase_order(context, invoice.po_id)
    if order is None:
        return [f"{invoice.id}: purchase order {invoice.po_id} is missing"]
    failures: list[str] = []
    if order.vendor_id != invoice.vendor_id:
        failures.append(
            f"{invoice.id}: purchase order {invoice.po_id} belongs to another vendor"
        )
    if order.amount_cents != invoice.amount_cents:
        failures.append(
            f"{invoice.id}: amount {invoice.amount_cents / 100:,.2f} does not match "
            f"purchase order {invoice.po_id}"
        )
    if order.currency.upper() != invoice.currency.upper():
        failures.append(
            f"{invoice.id}: currency {invoice.currency} does not match purchase "
            f"order {invoice.po_id} ({order.currency})"
        )
    return failures


def _po_reference(data: dict[str, Any]) -> str | None:
    """The PO reference from an extraction observation, when the source cites one."""
    fields = data.get("fields") or {}
    reference = fields.get("po_reference") if isinstance(fields, dict) else None
    value = reference.get("value") if isinstance(reference, dict) else reference
    if isinstance(value, str) and value.strip():
        return value.strip()
    parsed = data.get("parsed") or {}
    parsed_reference = parsed.get("po_reference") if isinstance(parsed, dict) else None
    if isinstance(parsed_reference, str) and parsed_reference.strip():
        return parsed_reference.strip()
    return None


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
) -> tuple[Vendor | None, str | None]:
    """Resolve the system-of-record vendor a vendor creation claim names."""
    data = (entry.result or {}).get("data") or {}
    vendor_id = data.get("vendor_id")
    if not isinstance(vendor_id, str) or not vendor_id:
        return None, f"{entry.key}: the creation action names no vendor id"
    try:
        vendor = context.accounting.get_vendor(vendor_id)
    except NotFoundError:
        return None, (
            f"{context.accounting.display_name} has 0 vendor(s) with id {vendor_id}, expected one"
        )
    return vendor, None


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
        assert vendor is not None
        vendor_id = vendor.id
        duplicates = _vendors(context, tax_id=vendor.tax_id, name=vendor.name)
        if len(duplicates) != 1 or duplicates[0].id != vendor_id:
            failures.append(
                f"{vendor_id}: tax id {vendor.tax_id} or name {vendor.name!r} "
                f"appears on {len(duplicates)} vendors, expected exactly one"
            )
            continue
        sources = _find_tax_forms(context, vendor.tax_id)
        if not sources:
            failures.append(
                f"{vendor_id}: no tax form with tax id {vendor.tax_id} was found"
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
                f"{vendor.tax_id} disagree on the vendor fields"
            )
            continue
        source, parsed = sources[0]
        reference = source.relative_to(context.shared_root).as_posix()
        for field in match_fields:
            actual = getattr(vendor, field, None)
            if parsed.get(field) != actual:
                failures.append(
                    f"{vendor_id}: {field} {actual!r} does not match "
                    f"the tax form {parsed.get(field)!r}"
                )
        if not any(failure.startswith(vendor_id) for failure in failures):
            evidence.extend([f"erp:{vendor_id}", f"source:{reference}"])
    if failures:
        return CheckOutcome(False, "; ".join(failures), tuple(evidence))
    return CheckOutcome(
        True,
        f"{len(claims)} created vendor(s) agree with their tax forms and "
        f"{context.accounting.display_name} records",
        tuple(evidence),
    )


CHECKS: dict[str, CheckFunction] = {
    "accounting_invoice_matches": check_accounting_invoice_matches,
    "erp_invoice_matches": check_erp_invoice_matches,
    "erp_payment_state": check_erp_payment_state,
    "erp_vendor_matches": check_erp_vendor_matches,
    "file_archived": check_file_archived,
    "india_bill_matches": check_india_bill_matches,
}


def verify_run(
    run_id: str,
    task_pack: TaskPack,
    store: RunStore,
    *,
    accounting: AccountingReads,
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
        accounting=accounting,
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
