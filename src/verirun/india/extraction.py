"""The India GST invoice schema and the normalisation of what a model reads.

A model returns one object per field: the value it read, its confidence, and the
exact line it copied the value from. Normalisation turns those strings into the
typed values the engine compares: paise, ISO dates, an uppercase GSTIN, and INR.
Citations are checked against the document text, so a fabricated quote is a
failed extraction rather than a plausible-looking field.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from verirun.india.validation import Validation, validate_invoice
from verirun.tools.base import ToolError

FIELD_ORDER = (
    "supplier_name",
    "supplier_gstin",
    "invoice_number",
    "invoice_date",
    "taxable_amount",
    "cgst",
    "sgst",
    "igst",
    "total",
    "hsn_sac",
    "po_reference",
    "due_date",
    "currency",
)
REQUIRED_FIELDS = (
    "supplier_name",
    "supplier_gstin",
    "invoice_number",
    "invoice_date",
    "taxable_amount",
    "total",
    "currency",
)
OPTIONAL_FIELDS = tuple(name for name in FIELD_ORDER if name not in REQUIRED_FIELDS)
AMOUNT_FIELDS = {
    "taxable_amount": "taxable_amount_paise",
    "cgst": "cgst_paise",
    "sgst": "sgst_paise",
    "igst": "igst_paise",
    "total": "total_paise",
}
DATE_FIELDS = ("invoice_date", "due_date")
ABSENT_VALUES = {"", "none", "null", "n/a", "na", "-"}
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y")
CURRENCY_MARKS = ("INR", "RS.", "RS", "₹")
WHITESPACE = re.compile(r"\s+")
DATE_TOKEN = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}")

EXTRACTION_SYSTEM_PROMPT = """\
You are the invoice extraction step of Verirun. You read India GST supplier
invoices and return their fields as JSON. You never guess: you report only what
the document shows, quote the exact line that supports every value, and state
how certain you are of each field.
"""


class ExtractedField(BaseModel):
    """One field: the value, the model's confidence, and the supporting quote."""

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    citation: str | None = None


class IndiaInvoiceFields(BaseModel):
    """The strict schema the extraction model must return."""

    model_config = ConfigDict(extra="forbid")

    supplier_name: ExtractedField
    supplier_gstin: ExtractedField
    invoice_number: ExtractedField
    invoice_date: ExtractedField
    taxable_amount: ExtractedField
    cgst: ExtractedField
    sgst: ExtractedField
    igst: ExtractedField
    total: ExtractedField
    hsn_sac: ExtractedField
    po_reference: ExtractedField
    due_date: ExtractedField
    currency: ExtractedField


def extraction_prompt() -> str:
    schema = json.dumps(IndiaInvoiceFields.model_json_schema(), indent=2)
    return (
        "Read the India GST invoice text and reply with one JSON object that "
        "follows this JSON schema exactly:\n"
        f"{schema}\n"
        "Rules:\n"
        "- Read only what the document shows. Never guess a value.\n"
        '- Every field has "value", "confidence" (0 to 1), and "citation".\n'
        "- \"citation\" must be the exact line from the invoice that supports the "
        "value, copied character for character. Use null when there is no value.\n"
        "- Required fields: supplier_name, supplier_gstin, invoice_number, "
        "invoice_date, taxable_amount, total, currency. Use null only when the "
        "document does not show the field at all.\n"
        '- Amounts are digits, commas, and a decimal point only, for example '
        '"10,000.00", with no currency symbol or code.\n'
        '- Dates are copied as printed, for example "05/09/2026".\n'
        "- cgst, sgst, and igst are the tax amounts, not the tax rates. Use null "
        "for a component the invoice does not charge.\n"
        "- hsn_sac and po_reference are the identifiers printed on the invoice, "
        "or null when the invoice does not cite one.\n"
        '- currency is the ISO code, for example "INR".\n'
    )


def raw_fields(fields: IndiaInvoiceFields) -> dict[str, dict[str, Any]]:
    """The model's words and confidences, before any normalisation."""
    return {
        name: {
            "value": getattr(fields, name).value,
            "confidence": getattr(fields, name).confidence,
            "citation": getattr(fields, name).citation,
        }
        for name in FIELD_ORDER
    }


def normalized_fields(fields: IndiaInvoiceFields) -> dict[str, dict[str, Any]]:
    """Typed values and confidences, keyed for the engine and Evidence Pack."""
    values = {name: _clean(getattr(fields, name).value) for name in FIELD_ORDER}
    missing = [name for name in REQUIRED_FIELDS if values[name] is None]
    if missing:
        raise ToolError(
            "invalid",
            f"the extraction did not read required field(s): {', '.join(missing)}",
        )
    currency = (values["currency"] or "").upper()
    if currency != "INR":
        raise ToolError(
            "invalid",
            f"the India GST path needs an INR invoice, got currency {values['currency']!r}",
        )
    normalized: dict[str, dict[str, Any]] = {}
    for name in FIELD_ORDER:
        confidence = getattr(fields, name).confidence
        citation = _clean(getattr(fields, name).citation)
        value: Any = values[name]
        if name in AMOUNT_FIELDS:
            paise = _paise(value, name) if value is not None else None
            normalized[AMOUNT_FIELDS[name]] = {
                "value": paise,
                "confidence": confidence,
                "citation": citation,
            }
            continue
        if name in DATE_FIELDS:
            value = _iso_date(value, name) if value is not None else None
        if name == "supplier_gstin" and value is not None:
            value = value.upper().replace(" ", "")
        if name == "currency":
            value = currency
        normalized[name] = {
            "value": value,
            "confidence": confidence,
            "citation": citation,
        }
    _require_positive(normalized, "taxable_amount_paise", "taxable_amount")
    _require_positive(normalized, "total_paise", "total")
    return normalized


def low_confidence_fields(fields: IndiaInvoiceFields, threshold: float) -> list[str]:
    """Every present field the model is less sure of than the threshold."""
    return [
        name
        for name in FIELD_ORDER
        if _clean(getattr(fields, name).value) is not None
        and getattr(fields, name).confidence < threshold
    ]


def min_confidence(fields: IndiaInvoiceFields) -> float:
    """The lowest confidence among the fields the model actually read."""
    present = [
        getattr(fields, name).confidence
        for name in FIELD_ORDER
        if _clean(getattr(fields, name).value) is not None
    ]
    return min(present) if present else 0.0


def citation_problems(
    fields: IndiaInvoiceFields, document_text: str
) -> list[tuple[str, str]]:
    """Every present field whose citation is empty, unsupported, or not in the document."""
    normalized_text = _normalise_text(document_text)
    problems: list[tuple[str, str]] = []
    for name in FIELD_ORDER:
        field = getattr(fields, name)
        if _clean(field.value) is None:
            continue
        citation = _clean(field.citation)
        if citation is None:
            problems.append((name, "the field has no citation"))
            continue
        if _normalise_text(citation) not in normalized_text:
            problems.append((name, "the citation is not in the document text"))
            continue
        if name not in DATE_FIELDS and not _supports_value(field.value, citation):
            problems.append((name, "the citation does not contain the value"))
        elif name in DATE_FIELDS and not _supports_date(field.value, citation):
            problems.append((name, "the citation does not contain the date"))
    return problems


def validate_fields(normalized: dict[str, dict[str, Any]]) -> list[Validation]:
    """Run the deterministic GST checks on the normalised values."""
    return validate_invoice(
        supplier_gstin=normalized["supplier_gstin"]["value"],
        taxable_paise=normalized["taxable_amount_paise"]["value"],
        cgst_paise=normalized["cgst_paise"]["value"],
        sgst_paise=normalized["sgst_paise"]["value"],
        igst_paise=normalized["igst_paise"]["value"],
        total_paise=normalized["total_paise"]["value"],
    )


def parsed_invoice(normalized: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The extracted invoice reduced to what matching and verification use."""
    return {
        "supplier_name": normalized["supplier_name"]["value"],
        "supplier_gstin": normalized["supplier_gstin"]["value"],
        "invoice_number": normalized["invoice_number"]["value"],
        "invoice_date": normalized["invoice_date"]["value"],
        "total_paise": normalized["total_paise"]["value"],
        "currency": normalized["currency"]["value"],
    }


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return None if text.lower() in ABSENT_VALUES else text


def _paise(raw: str, field: str) -> int:
    text = raw.strip().replace(",", "").replace(" ", "")
    for mark in CURRENCY_MARKS:
        text = re.sub(re.escape(mark), "", text, flags=re.IGNORECASE)
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ToolError("invalid", f"{field} is not a number: {raw!r}") from exc
    paise = value * 100
    if value < 0 or paise != paise.to_integral_value():
        raise ToolError(
            "invalid",
            f"{field} must be a non-negative amount with at most two decimals: {raw!r}",
        )
    return int(paise)


def _iso_date(raw: str, field: str) -> str:
    parsed = _parse_date(raw)
    if parsed is None:
        raise ToolError("invalid", f"{field} is not a readable date: {raw!r}")
    return parsed.isoformat()


def _parse_date(raw: str) -> date | None:
    text = raw.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for pattern in DATE_FORMATS:
        try:
            return date(*time.strptime(text, pattern)[:3])
        except ValueError:
            continue
    return None


def _require_positive(
    normalized: dict[str, dict[str, Any]], key: str, field: str
) -> None:
    value = normalized[key]["value"]
    if value is None or value <= 0:
        raise ToolError("invalid", f"{field} must be greater than zero, got {value!r}")


def _normalise_text(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip().lower()


def _compact(text: str | None) -> str:
    """Ignore spacing, commas, and the rupee sign when comparing a value."""
    return re.sub(r"[\s,₹]+", "", text or "").lower()


def _supports_value(value: str | None, citation: str | None) -> bool:
    """The quoted line must contain the value it is cited for.

    Amounts are compared on the digits and decimal point, with a boundary check
    so ``10,000.00`` does not match inside ``110,000.00``.
    """
    wanted = _compact(value)
    if not wanted:
        return False
    pattern = re.escape(wanted)
    if wanted[:1].isdigit() and wanted[-1:].isdigit():
        pattern = rf"(?<![\d.]){pattern}(?![\d.])"
    return re.search(pattern, _compact(citation)) is not None


def _supports_date(value: str | None, citation: str | None) -> bool:
    """The citation must name the same calendar date as the field.

    The model is asked to copy the printed date, but the check also accepts a
    normalized ISO value against a printed date so a reformatted value is not a
    false failure.
    """
    target = _parse_date(value or "")
    if target is None:
        return _supports_value(value, citation)
    for token in DATE_TOKEN.findall(citation or ""):
        if _parse_date(token) == target:
            return True
    return False
