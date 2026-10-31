"""Tests for the India GST extraction tool.

The tool is a Tool like any other: one call in, one Observation out. It reads a
text-layer PDF, asks the model for fields with citations and confidences, checks
the citations against the document, and runs the deterministic GST rules.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.india_fixture import (
    GSTIN,
    INVOICE_LINES,
    PDF_FILENAME,
    extraction_json,
    extraction_payload,
    write_invoice_pdf,
)
from tests.support import ScriptedClient
from verirun.tools.invoice import ExtractIndiaInvoiceTool

PATH = f"documents/invoices/{PDF_FILENAME}"


def tool(
    tmp_path: Path, reply: str | None = None, *, threshold: float = 0.8
) -> tuple[ExtractIndiaInvoiceTool, Path]:
    shared = tmp_path / "shared"
    pdf = write_invoice_pdf(shared / PATH)
    client = ScriptedClient([reply if reply is not None else extraction_json()])
    return (
        ExtractIndiaInvoiceTool(shared, client, confidence_threshold=threshold),
        pdf,
    )


def test_extraction_returns_every_field_with_confidence_and_citation(
    tmp_path: Path,
) -> None:
    extractor, _pdf = tool(tmp_path)

    observation = extractor.invoke({"path": PATH})

    assert observation.ok, observation.summary
    data = observation.data
    assert data["path"] == PATH
    assert data["method"] == "text"
    assert data["schema"] == "india-gst"
    assert data["pages"] == 1
    assert data["threshold"] == 0.8
    assert data["min_confidence"] == 0.95
    fields = data["fields"]
    assert fields["supplier_name"] == {
        "value": "Paper Trail Supplies Pvt Ltd",
        "confidence": 0.98,
        "citation": "Supplier: Paper Trail Supplies Pvt Ltd",
    }
    assert fields["taxable_amount_paise"]["value"] == 1_000_000
    assert fields["total_paise"]["value"] == 1_180_000
    assert fields["invoice_date"]["value"] == "2026-09-05"
    assert fields["currency"]["value"] == "INR"
    assert [item["id"] for item in data["validations"]] == [
        "gstin_checksum",
        "tax_sum",
        "tax_split",
    ]
    assert all(item["ok"] for item in data["validations"])
    assert data["parsed"]["invoice_number"] == "PTS/2026/0142"


def test_a_field_below_the_threshold_is_a_low_confidence_failure(
    tmp_path: Path,
) -> None:
    payload = extraction_payload()
    payload["total"]["confidence"] = 0.42
    extractor, _pdf = tool(tmp_path, json.dumps(payload))

    observation = extractor.invoke({"path": PATH})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "below the confidence threshold" in observation.summary
    assert observation.data["low_confidence"] is True
    assert observation.data["low_confidence_fields"] == ["total"]
    assert observation.data["fields"]["total_paise"]["confidence"] == 0.42
    assert observation.data["validations"]


def test_a_citation_that_is_not_in_the_document_fails_the_extraction(
    tmp_path: Path,
) -> None:
    payload = extraction_payload()
    payload["total"]["citation"] = "Total: 99,999.00 INR"
    extractor, _pdf = tool(tmp_path, json.dumps(payload))

    observation = extractor.invoke({"path": PATH})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "citation" in observation.summary
    assert [name for name, _ in observation.data["citation_problems"]] == ["total"]


def test_a_bad_gstin_is_recorded_as_a_failed_validation(tmp_path: Path) -> None:
    bad_gstin = "29AABCV1234C1ZP"
    shared = tmp_path / "shared"
    write_invoice_pdf(
        shared / PATH,
        lines=tuple(line.replace(GSTIN, bad_gstin) for line in INVOICE_LINES),
    )
    payload = extraction_payload()
    payload["supplier_gstin"] = {
        "value": bad_gstin,
        "confidence": 0.99,
        "citation": f"GSTIN: {bad_gstin}",
    }
    script = ScriptedClient([json.dumps(payload)])
    extractor = ExtractIndiaInvoiceTool(shared, script, confidence_threshold=0.8)

    observation = extractor.invoke({"path": PATH})

    assert observation.ok
    validations = {item["id"]: item for item in observation.data["validations"]}
    assert validations["gstin_checksum"]["ok"] is False
    assert "check digit" in validations["gstin_checksum"]["detail"]
    assert validations["tax_sum"]["ok"] is True


def test_a_tax_sum_mismatch_is_recorded_as_a_failed_validation(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    write_invoice_pdf(
        shared / PATH,
        lines=(*INVOICE_LINES[:-1], "Total: 11,850.00 INR"),
    )
    payload = extraction_payload()
    payload["total"] = {
        "value": "11,850.00",
        "confidence": 0.98,
        "citation": "Total: 11,850.00 INR",
    }
    payload["currency"]["citation"] = "Total: 11,850.00 INR"
    script = ScriptedClient([json.dumps(payload)])
    extractor = ExtractIndiaInvoiceTool(shared, script, confidence_threshold=0.8)

    observation = extractor.invoke({"path": PATH})

    assert observation.ok
    validations = {item["id"]: item for item in observation.data["validations"]}
    assert validations["tax_sum"]["ok"] is False
    assert "11,850.00 INR" in validations["tax_sum"]["detail"]


def test_a_pdf_without_a_text_layer_points_at_the_vision_path(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    write_invoice_pdf(shared / PATH, lines=())
    script = ScriptedClient([])
    extractor = ExtractIndiaInvoiceTool(shared, script, confidence_threshold=0.8)

    observation = extractor.invoke({"path": PATH})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "no text layer" in observation.summary
    assert "files.extract" in observation.summary
    assert script.calls == []


def test_a_non_pdf_is_rejected(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "invoice.txt").write_text("not a pdf", encoding="utf-8")
    script = ScriptedClient([])
    extractor = ExtractIndiaInvoiceTool(shared, script, confidence_threshold=0.8)

    observation = extractor.invoke({"path": "invoice.txt"})

    assert not observation.ok
    assert observation.error_kind == "invalid"


def test_malformed_model_output_is_an_invalid_extraction(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    write_invoice_pdf(shared / PATH)
    script = ScriptedClient(["not json at all", "still not json"])
    extractor = ExtractIndiaInvoiceTool(shared, script, confidence_threshold=0.8)

    observation = extractor.invoke({"path": PATH})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "could not extract" in observation.summary
    assert observation.data["method"] == "text"
    assert observation.data["cost_usd"] >= 0
