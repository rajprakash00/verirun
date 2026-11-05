"""The India GST eval universe: one fake mailbox and accounting system per case.

The scenarios share one supplier and, where a case cites one, one
purchase-order id, but each case prints its own invoice number, amounts, GSTIN,
and purchase-order reference onto a reproducible text-layer PDF. The accounting
fake is seeded with the matching vendor and purchase order, and with a duplicate
bill where the case needs one.

Nothing here touches the Mock Suite or a network. The PDFs are real files under
the scenario's own document root because the extraction tool and the Verifier
read them independently.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from fpdf import FPDF

from verirun.adapters import (
    Bill,
    InMemoryAccountingSystem,
    InMemoryMailSource,
    MailAttachment,
    MailMessage,
    PurchaseOrder,
    Vendor,
)
from verirun.india.validation import gstin_check_digit
from verirun.runtime import Backends

SUPPLIER = "Paper Trail Supplies Pvt Ltd"
VENDOR_ID = "V-IN-1001"
VENDOR_EMAIL = "accounts@papertrail.example"
VENDOR_ADDRESS = "14 Market Road, Bengaluru"
CREATED_AT = "2026-09-01T09:00:00+00:00"
RECEIVED_AT = "2026-09-05T09:00:00+00:00"

GSTIN = "29AABCV1234C1Z" + gstin_check_digit("29AABCV1234C1Z")
OTHER_GSTIN = "27AABCV1234C1Z" + gstin_check_digit("27AABCV1234C1Z")


@dataclass(frozen=True)
class InvoiceCase:
    """One seeded India GST invoice and the accounting state around it."""

    key: str
    title: str
    invoice_number: str
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    total_paise: int
    po_reference: str | None = "PO-IN-2001"
    invoice_gstin: str = GSTIN
    vendor_gstin: str = GSTIN
    existing_bill: bool = False
    transient_failures: int = 0
    confidence: dict[str, float] = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        return f"MSG-IN-{self.key.upper()}"

    @property
    def filename(self) -> str:
        return self.invoice_number.replace("/", "-") + ".pdf"

    @property
    def attachment_path(self) -> str:
        return f"documents/invoices/{self.filename}"


INDIA_CASES: dict[str, InvoiceCase] = {
    "india_happy": InvoiceCase(
        key="india_happy",
        title="India GST happy path",
        invoice_number="PTS/2026/0142",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
    ),
    "india_duplicate": InvoiceCase(
        key="india_duplicate",
        title="Duplicate GST invoice",
        invoice_number="PTS/2026/0143",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
        existing_bill=True,
    ),
    "india_gstin_mismatch": InvoiceCase(
        key="india_gstin_mismatch",
        title="Supplier GSTIN mismatch",
        invoice_number="PTS/2026/0144",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
        invoice_gstin=OTHER_GSTIN,
    ),
    "india_tax_mismatch": InvoiceCase(
        key="india_tax_mismatch",
        title="GST tax arithmetic mismatch",
        invoice_number="PTS/2026/0145",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=930_000,
    ),
    "india_missing_po": InvoiceCase(
        key="india_missing_po",
        title="Missing purchase order behind approval",
        invoice_number="PTS/2026/0146",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
        po_reference=None,
    ),
    "india_over_limit": InvoiceCase(
        key="india_over_limit",
        title="Over-limit purchase bill behind approval",
        invoice_number="PTS/2026/0147",
        taxable_paise=1_000_000,
        cgst_paise=90_000,
        sgst_paise=90_000,
        igst_paise=0,
        total_paise=1_180_000,
    ),
    "india_low_confidence": InvoiceCase(
        key="india_low_confidence",
        title="Low-confidence extraction behind approval",
        invoice_number="PTS/2026/0148",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
        confidence={"total": 0.62},
    ),
    "india_transient": InvoiceCase(
        key="india_transient",
        title="Transient Zoho failure",
        invoice_number="PTS/2026/0149",
        taxable_paise=800_000,
        cgst_paise=72_000,
        sgst_paise=72_000,
        igst_paise=0,
        total_paise=944_000,
        transient_failures=1,
    ),
}


def money(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def invoice_lines(case: InvoiceCase) -> tuple[str, ...]:
    """The printed text layer the extraction tool and the Verifier both read."""
    lines = [
        "TAX INVOICE",
        f"Supplier: {SUPPLIER}",
        f"GSTIN: {case.invoice_gstin}",
        f"Invoice number: {case.invoice_number}",
        "Invoice date: 05/09/2026",
        "Due date: 05/10/2026",
    ]
    if case.po_reference:
        lines.append(f"Purchase order: {case.po_reference}")
    lines.extend(
        [
            "HSN/SAC: 4820",
            f"Taxable amount: {money(case.taxable_paise)}",
            f"CGST 9%: {money(case.cgst_paise)}",
            f"SGST 9%: {money(case.sgst_paise)}",
            f"IGST: {money(case.igst_paise)}",
            f"Total: {money(case.total_paise)} INR",
        ]
    )
    return tuple(lines)


def write_invoice_pdf(path: str | Path, case: InvoiceCase) -> Path:
    """Write one reproducible text-layer invoice PDF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC))
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    y = 20
    for line in invoice_lines(case):
        pdf.text(15, y, line)
        y += 8
    pdf.output(str(path))
    return path


def extraction_payload(case: InvoiceCase) -> dict[str, dict[str, object]]:
    """The strict-schema JSON a scripted model returns for this invoice."""
    lines = invoice_lines(case)

    def cited(prefix: str) -> str:
        return next(line for line in lines if line.startswith(prefix))

    payload: dict[str, dict[str, object]] = {
        "supplier_name": {
            "value": SUPPLIER,
            "confidence": 0.98,
            "citation": cited("Supplier:"),
        },
        "supplier_gstin": {
            "value": case.invoice_gstin,
            "confidence": 0.99,
            "citation": cited("GSTIN:"),
        },
        "invoice_number": {
            "value": case.invoice_number,
            "confidence": 0.99,
            "citation": cited("Invoice number:"),
        },
        "invoice_date": {
            "value": "05/09/2026",
            "confidence": 0.97,
            "citation": cited("Invoice date:"),
        },
        "due_date": {
            "value": "05/10/2026",
            "confidence": 0.95,
            "citation": cited("Due date:"),
        },
        "taxable_amount": {
            "value": money(case.taxable_paise),
            "confidence": 0.98,
            "citation": cited("Taxable amount:"),
        },
        "cgst": {
            "value": money(case.cgst_paise),
            "confidence": 0.98,
            "citation": cited("CGST"),
        },
        "sgst": {
            "value": money(case.sgst_paise),
            "confidence": 0.98,
            "citation": cited("SGST"),
        },
        "igst": {
            "value": money(case.igst_paise),
            "confidence": 0.97,
            "citation": cited("IGST"),
        },
        "total": {
            "value": money(case.total_paise),
            "confidence": 0.98,
            "citation": cited("Total:"),
        },
        "hsn_sac": {
            "value": "4820",
            "confidence": 0.96,
            "citation": cited("HSN/SAC:"),
        },
        "currency": {
            "value": "INR",
            "confidence": 0.99,
            "citation": cited("Total:"),
        },
        "po_reference": (
            {
                "value": case.po_reference,
                "confidence": 0.97,
                "citation": cited("Purchase order:"),
            }
            if case.po_reference
            else {"value": None, "confidence": 0.0, "citation": None}
        ),
    }
    for name, confidence in case.confidence.items():
        payload[name]["confidence"] = confidence
    return payload


def extraction_json(case: InvoiceCase) -> str:
    return json.dumps(extraction_payload(case))


def build_india_backends(case: InvoiceCase, root: str | Path) -> Backends:
    """Build the fake mailbox and accounting system for one India scenario."""
    shared = Path(root) / "shared"
    shutil.rmtree(shared, ignore_errors=True)
    pdf = write_invoice_pdf(shared / case.attachment_path, case)
    content = pdf.read_bytes()
    attachment = MailAttachment(
        id=sha256(content).hexdigest(),
        filename=pdf.name,
        content_type="application/pdf",
        size_bytes=len(content),
        path=case.attachment_path,
    )
    message = MailMessage(
        id=case.message_id,
        folder="inbox",
        sender="Paper Trail Accounts",
        sender_email=VENDOR_EMAIL,
        subject=f"Tax invoice {case.invoice_number} from {SUPPLIER}",
        body=(
            "Hello,\n\n"
            f"Please process the attached tax invoice {case.invoice_number}.\n\n"
            "Regards,\nPaper Trail Accounts"
        ),
        received_at=RECEIVED_AT,
        attachments=(attachment,),
        scenario=case.key,
    )
    mail = InMemoryMailSource(shared, [message])
    accounting = InMemoryAccountingSystem(
        vendors=[
            Vendor(
                id=VENDOR_ID,
                name=SUPPLIER,
                tax_id=case.vendor_gstin,
                email=VENDOR_EMAIL,
                address=VENDOR_ADDRESS,
                status="active",
                created_at=CREATED_AT,
            )
        ],
        purchase_orders=(
            [
                PurchaseOrder(
                    id=case.po_reference,
                    number=case.po_reference,
                    vendor_id=VENDOR_ID,
                    amount_cents=case.total_paise,
                    currency="INR",
                    status="open",
                    issued_at=CREATED_AT,
                )
            ]
            if case.po_reference
            else []
        ),
        bills=(
            [
                Bill(
                    id="INV-IN-3001",
                    number=case.invoice_number,
                    vendor_id=VENDOR_ID,
                    po_id=case.po_reference,
                    gr_id=None,
                    amount_cents=case.total_paise,
                    currency="INR",
                    status="received",
                    received_at=CREATED_AT,
                )
            ]
            if case.existing_bill
            else []
        ),
        transient_bill_failures=case.transient_failures,
    )
    return Backends(mail=mail, accounting=accounting, reads=accounting)
