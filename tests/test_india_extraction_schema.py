"""Tests for the India GST extraction schema: values, confidence, citations."""

from __future__ import annotations

import json

import pytest

from tests.india_fixture import INVOICE_TEXT, extraction_json
from verirun.india import validate_invoice
from verirun.india.extraction import (
    IndiaInvoiceFields,
    citation_problems,
    low_confidence_fields,
    min_confidence,
    normalized_fields,
    parsed_invoice,
)
from verirun.tools.base import ToolError


def fields(**overrides: dict) -> IndiaInvoiceFields:
    payload = json.loads(extraction_json())
    for name, value in overrides.items():
        payload[name] = value
    return IndiaInvoiceFields.model_validate(payload)


def test_every_schema_field_is_extracted_with_confidence_and_citation() -> None:
    extracted = fields()

    normalized = normalized_fields(extracted)

    expected = {
        "supplier_name",
        "supplier_gstin",
        "invoice_number",
        "invoice_date",
        "taxable_amount_paise",
        "cgst_paise",
        "sgst_paise",
        "igst_paise",
        "total_paise",
        "hsn_sac",
        "po_reference",
        "due_date",
        "currency",
    }
    assert set(normalized) == expected
    for name, field in normalized.items():
        assert "confidence" in field, name
        assert "value" in field, name
        assert "citation" in field, name
    assert normalized["supplier_name"]["value"] == "Paper Trail Supplies Pvt Ltd"
    assert normalized["supplier_gstin"]["value"] == "29AABCV1234C1ZQ"
    assert normalized["invoice_number"]["value"] == "PTS/2026/0142"
    assert normalized["invoice_date"]["value"] == "2026-09-05"
    assert normalized["due_date"]["value"] == "2026-10-05"
    assert normalized["taxable_amount_paise"]["value"] == 1_000_000
    assert normalized["cgst_paise"]["value"] == 90_000
    assert normalized["sgst_paise"]["value"] == 90_000
    assert normalized["igst_paise"]["value"] == 0
    assert normalized["total_paise"]["value"] == 1_180_000
    assert normalized["currency"]["value"] == "INR"
    assert normalized["hsn_sac"]["value"] == "4820"
    assert normalized["po_reference"]["value"] == "PO-IN-2001"


def test_every_present_field_cites_text_from_the_document() -> None:
    extracted = fields()

    assert citation_problems(extracted, INVOICE_TEXT) == []


def test_a_citation_that_is_not_in_the_document_is_caught() -> None:
    extracted = fields(
        supplier_gstin={
            "value": "29AABCV1234C1ZQ",
            "confidence": 0.99,
            "citation": "GSTIN: 29XXXXX0000X1Z0",
        }
    )

    problems = citation_problems(extracted, INVOICE_TEXT)

    assert len(problems) == 1
    assert problems[0][0] == "supplier_gstin"
    assert "not in the document" in problems[0][1]


def test_a_citation_that_does_not_support_the_value_is_caught() -> None:
    extracted = fields(
        total={
            "value": "11,850.00",
            "confidence": 0.98,
            "citation": "Total: 11,800.00 INR",
        }
    )

    problems = citation_problems(extracted, INVOICE_TEXT)

    assert len(problems) == 1
    assert problems[0][0] == "total"
    assert "does not contain the value" in problems[0][1]


def test_an_amount_does_not_match_inside_a_larger_amount() -> None:
    extracted = fields(
        taxable_amount={
            "value": "10,000.00",
            "confidence": 0.98,
            "citation": "Total: 110,000.00 INR",
        }
    )

    problems = citation_problems(extracted, "Total: 110,000.00 INR")

    assert ("taxable_amount", "the citation does not contain the value") in problems


def test_a_date_citation_must_name_the_same_date() -> None:
    extracted = fields(
        invoice_date={
            "value": "05/09/2026",
            "confidence": 0.97,
            "citation": "Due date: 05/10/2026",
        }
    )

    problems = citation_problems(extracted, "Due date: 05/10/2026")

    assert ("invoice_date", "the citation does not contain the date") in problems


def test_a_present_field_must_have_a_citation() -> None:
    extracted = fields(total={"value": "11,800.00", "confidence": 0.98, "citation": None})

    problems = citation_problems(extracted, INVOICE_TEXT)

    assert ("total", "the field has no citation") in problems


def test_an_absent_optional_field_needs_no_citation() -> None:
    extracted = fields(
        po_reference={"value": None, "confidence": 0.3, "citation": None}
    )

    assert citation_problems(extracted, INVOICE_TEXT) == []
    assert low_confidence_fields(extracted, 0.8) == []


def test_a_low_confidence_present_field_is_flagged() -> None:
    extracted = fields(
        total={"value": "11,800.00", "confidence": 0.42, "citation": "Total: 11,800.00 INR"}
    )

    assert low_confidence_fields(extracted, 0.8) == ["total"]
    assert min_confidence(extracted) == 0.42


def test_a_missing_required_field_is_an_invalid_extraction() -> None:
    extracted = fields(
        supplier_gstin={"value": None, "confidence": 0.99, "citation": None}
    )

    with pytest.raises(ToolError) as excinfo:
        normalized_fields(extracted)

    assert excinfo.value.kind == "invalid"
    assert "supplier_gstin" in excinfo.value.message


def test_a_non_inr_invoice_is_rejected_for_the_india_path() -> None:
    extracted = fields(
        currency={"value": "USD", "confidence": 0.99, "citation": "Total: 11,800.00 INR"}
    )

    with pytest.raises(ToolError) as excinfo:
        normalized_fields(extracted)

    assert excinfo.value.kind == "invalid"
    assert "INR" in excinfo.value.message


def test_an_unreadable_date_is_rejected() -> None:
    extracted = fields(
        invoice_date={
            "value": "sometime in September",
            "confidence": 0.9,
            "citation": "Invoice date: 05/09/2026",
        }
    )

    with pytest.raises(ToolError) as excinfo:
        normalized_fields(extracted)

    assert excinfo.value.kind == "invalid"
    assert "invoice_date" in excinfo.value.message


def test_an_unreadable_amount_is_rejected() -> None:
    extracted = fields(
        taxable_amount={
            "value": "ten thousand",
            "confidence": 0.9,
            "citation": "Taxable amount: 10,000.00",
        }
    )

    with pytest.raises(ToolError) as excinfo:
        normalized_fields(extracted)

    assert excinfo.value.kind == "invalid"
    assert "not a number" in excinfo.value.message


def test_a_negative_or_zero_total_is_rejected() -> None:
    negative = fields(
        total={"value": "-1.00", "confidence": 0.9, "citation": "Total: 11,800.00 INR"}
    )
    zero = fields(
        total={"value": "0.00", "confidence": 0.9, "citation": "Total: 11,800.00 INR"}
    )

    with pytest.raises(ToolError, match="non-negative"):
        normalized_fields(negative)
    with pytest.raises(ToolError, match="greater than zero"):
        normalized_fields(zero)


def test_normalised_values_feed_the_gst_validations() -> None:
    normalized = normalized_fields(fields())

    results = validate_invoice(
        supplier_gstin=normalized["supplier_gstin"]["value"],
        taxable_paise=normalized["taxable_amount_paise"]["value"],
        cgst_paise=normalized["cgst_paise"]["value"],
        sgst_paise=normalized["sgst_paise"]["value"],
        igst_paise=normalized["igst_paise"]["value"],
        total_paise=normalized["total_paise"]["value"],
    )

    assert [result.id for result in results] == ["gstin_checksum", "tax_sum", "tax_split"]
    assert all(result.ok for result in results), [result.detail for result in results]


def test_parsed_invoice_is_what_matching_and_verification_compare() -> None:
    parsed = parsed_invoice(normalized_fields(fields()))

    assert parsed == {
        "supplier_name": "Paper Trail Supplies Pvt Ltd",
        "supplier_gstin": "29AABCV1234C1ZQ",
        "invoice_number": "PTS/2026/0142",
        "invoice_date": "2026-09-05",
        "total_paise": 1_180_000,
        "currency": "INR",
    }
