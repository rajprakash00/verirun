"""Read an India GST tax invoice from its own text layer.

The Verifier uses this parser instead of the executor's extraction. It is
deliberately small and strict: the printed lines a tax invoice always carries
are read directly, and a document that does not match is unreadable rather than
guessed at. The parsed values are used only to check the system-of-record bill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SUPPLIER = re.compile(r"^Supplier:\s*(.+?)\s*$", re.MULTILINE)
GSTIN = re.compile(r"^GSTIN:\s*(\S+)\s*$", re.MULTILINE)
NUMBER = re.compile(r"^Invoice number:\s*(\S+)\s*$", re.MULTILINE)
PURCHASE_ORDER = re.compile(r"^Purchase order:\s*(\S+)\s*$", re.MULTILINE)
TAXABLE = re.compile(r"^Taxable amount:\s*([\d,]+\.\d{2})\s*$", re.MULTILINE)
CGST = re.compile(r"^CGST[^:\n]*:\s*([\d,]+\.\d{2})\s*$", re.MULTILINE)
SGST = re.compile(r"^SGST[^:\n]*:\s*([\d,]+\.\d{2})\s*$", re.MULTILINE)
IGST = re.compile(r"^IGST[^:\n]*:\s*([\d,]+\.\d{2})\s*$", re.MULTILINE)
TOTAL = re.compile(
    r"^Total:\s*([\d,]+\.\d{2})\s*([A-Z]{3})\s*$", re.MULTILINE
)


@dataclass(frozen=True)
class SourceInvoice:
    """The printed facts of a supplier tax invoice, in engine units."""

    supplier_name: str
    supplier_gstin: str
    invoice_number: str
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    total_paise: int
    currency: str
    po_reference: str | None = None


def parse_source_invoice(path: str | Path) -> SourceInvoice:
    """Read one invoice PDF's text layer, or raise ``ValueError``."""
    from pypdf import PdfReader

    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(str(path)).pages
    )
    return parse_source_text(text)


def parse_source_text(text: str) -> SourceInvoice:
    """Read the invoice fields from extracted PDF text."""
    supplier = _required(SUPPLIER, text, "supplier")
    gstin = _required(GSTIN, text, "GSTIN")
    number = _required(NUMBER, text, "invoice number")
    taxable = _paise(_required(TAXABLE, text, "taxable amount"), "taxable amount")
    total_match = TOTAL.search(text)
    if total_match is None:
        raise ValueError("the source document has no readable total")
    total = _paise(total_match.group(1), "total")
    currency = total_match.group(2)
    order = PURCHASE_ORDER.search(text)
    return SourceInvoice(
        supplier_name=supplier,
        supplier_gstin=gstin.upper().replace(" ", ""),
        invoice_number=number,
        taxable_paise=taxable,
        cgst_paise=_optional_tax(CGST, text, "CGST"),
        sgst_paise=_optional_tax(SGST, text, "SGST"),
        igst_paise=_optional_tax(IGST, text, "IGST"),
        total_paise=total,
        currency=currency,
        po_reference=order.group(1) if order is not None else None,
    )


def find_source_invoice(
    root: str | Path, invoice_number: str
) -> tuple[Path, SourceInvoice] | None:
    """The first PDF under ``root`` whose printed number is ``invoice_number``.

    Matching the printed number, not the filename, keeps the check working when
    a supplier names an attachment by date or uses dashes for slashes.
    """
    from verirun.adapters.matching import reference_key

    wanted = reference_key(invoice_number)
    for path in sorted(Path(root).rglob("*.pdf")):
        try:
            parsed = parse_source_invoice(path)
        except (ValueError, OSError):
            continue
        if reference_key(parsed.invoice_number) == wanted:
            return path, parsed
    return None


def _required(pattern: re.Pattern[str], text: str, field: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"the source document has no readable {field}")
    return match.group(1).strip()


def _optional_tax(pattern: re.Pattern[str], text: str, field: str) -> int:
    match = pattern.search(text)
    return _paise(match.group(1), field) if match is not None else 0


def _paise(raw: str, field: str) -> int:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse the {field} {raw!r}") from exc
    paise = value * 100
    if paise != paise.to_integral_value():
        raise ValueError(f"the {field} {raw!r} has more than two decimals")
    return int(paise)


def invoice_as_dict(invoice: SourceInvoice) -> dict[str, Any]:
    """The parsed invoice as evidence-friendly JSON."""
    return {
        "supplier_name": invoice.supplier_name,
        "supplier_gstin": invoice.supplier_gstin,
        "invoice_number": invoice.invoice_number,
        "taxable_paise": invoice.taxable_paise,
        "cgst_paise": invoice.cgst_paise,
        "sgst_paise": invoice.sgst_paise,
        "igst_paise": invoice.igst_paise,
        "total_paise": invoice.total_paise,
        "currency": invoice.currency,
        "po_reference": invoice.po_reference,
    }
