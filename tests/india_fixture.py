"""Shared India GST fixture: one supplier, one tax invoice, one email.

The same invoice PDF is used by the IMAP fixture mailbox, the watched-folder
source, and the extraction tool tests, so the three paths are compared on the
same bytes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fpdf import FPDF

GSTIN = "29AABCV1234C1ZQ"
SUPPLIER = "Paper Trail Supplies Pvt Ltd"
INVOICE_NUMBER = "PTS/2026/0142"
PO_ID = "PO-IN-2001"
TOTAL_PAISE = 1_180_000
TAXABLE_PAISE = 1_000_000
PDF_FILENAME = "PTS-2026-0142.pdf"
PDF_CREATED_AT = datetime(2026, 9, 5, 9, 0, 0, tzinfo=UTC)

INVOICE_LINES = (
    "TAX INVOICE",
    f"Supplier: {SUPPLIER}",
    f"GSTIN: {GSTIN}",
    f"Invoice number: {INVOICE_NUMBER}",
    "Invoice date: 05/09/2026",
    "Due date: 05/10/2026",
    f"Purchase order: {PO_ID}",
    "HSN/SAC: 4820",
    "Taxable amount: 10,000.00",
    "CGST 9%: 900.00",
    "SGST 9%: 900.00",
    "IGST: 0.00",
    "Total: 11,800.00 INR",
)
INVOICE_TEXT = "\n".join(INVOICE_LINES)


def extraction_payload() -> dict[str, dict[str, Any]]:
    """The strict-schema JSON a model replies with for this invoice."""
    return {
        "supplier_name": {
            "value": SUPPLIER,
            "confidence": 0.98,
            "citation": f"Supplier: {SUPPLIER}",
        },
        "supplier_gstin": {
            "value": GSTIN,
            "confidence": 0.99,
            "citation": f"GSTIN: {GSTIN}",
        },
        "invoice_number": {
            "value": INVOICE_NUMBER,
            "confidence": 0.99,
            "citation": f"Invoice number: {INVOICE_NUMBER}",
        },
        "invoice_date": {
            "value": "05/09/2026",
            "confidence": 0.97,
            "citation": "Invoice date: 05/09/2026",
        },
        "due_date": {
            "value": "05/10/2026",
            "confidence": 0.95,
            "citation": "Due date: 05/10/2026",
        },
        "taxable_amount": {
            "value": "10,000.00",
            "confidence": 0.98,
            "citation": "Taxable amount: 10,000.00",
        },
        "cgst": {"value": "900.00", "confidence": 0.98, "citation": "CGST 9%: 900.00"},
        "sgst": {"value": "900.00", "confidence": 0.98, "citation": "SGST 9%: 900.00"},
        "igst": {"value": "0.00", "confidence": 0.97, "citation": "IGST: 0.00"},
        "total": {
            "value": "11,800.00",
            "confidence": 0.98,
            "citation": "Total: 11,800.00 INR",
        },
        "hsn_sac": {"value": "4820", "confidence": 0.96, "citation": "HSN/SAC: 4820"},
        "po_reference": {
            "value": PO_ID,
            "confidence": 0.97,
            "citation": f"Purchase order: {PO_ID}",
        },
        "currency": {
            "value": "INR",
            "confidence": 0.99,
            "citation": "Total: 11,800.00 INR",
        },
    }


def extraction_json(**overrides: dict[str, Any]) -> str:
    payload = extraction_payload()
    payload.update(overrides)
    return json.dumps(payload)


def write_invoice_pdf(
    path: str | Path, *, lines: tuple[str, ...] | None = None
) -> Path:
    """Write the text-layer invoice PDF with reproducible bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = FPDF()
    pdf.set_creation_date(PDF_CREATED_AT)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    y = 20
    for line in lines if lines is not None else INVOICE_LINES:
        pdf.text(15, y, line)
        y += 8
    pdf.output(str(path))
    return path


def write_email(
    path: str | Path,
    pdf_bytes: bytes,
    *,
    subject: str | None = None,
    message_id: str | None = None,
) -> Path:
    """Write one invoice email with the PDF attached."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    message = EmailMessage()
    message["From"] = "Paper Trail Accounts <accounts@papertrail.example>"
    message["To"] = "ap@company.example"
    message["Subject"] = subject or f"Tax invoice {INVOICE_NUMBER} from {SUPPLIER}"
    message["Date"] = "Sat, 05 Sep 2026 09:00:00 +0000"
    message["Message-ID"] = message_id or "<pts-2026-0142@papertrail.example>"
    message.set_content(
        "Hello,\n\n"
        f"Please process the attached tax invoice {INVOICE_NUMBER} "
        f"against purchase order {PO_ID}.\n\n"
        "Regards,\nPaper Trail Accounts"
    )
    message.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=PDF_FILENAME,
    )
    path.write_bytes(message.as_bytes())
    return path


def write_fixture_mailbox(directory: str | Path) -> list[Path]:
    """Build a small fixture mailbox of .eml files and return them in UID order."""
    directory = Path(directory)
    pdf = directory / "source" / PDF_FILENAME
    write_invoice_pdf(pdf)
    return [write_email(directory / "mailbox" / "01.eml", pdf.read_bytes())]
