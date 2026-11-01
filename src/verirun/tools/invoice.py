"""The India GST extraction tool: invoice.extract.

The tool reads a text-layer PDF, asks the reasoning model for every GST field
with a confidence and an exact citation, refuses citations that are not in the
document, and runs the deterministic GSTIN and tax validations. A field below
the Task Pack's confidence threshold fails the call; a failed validation does
not, because the extraction succeeded and the Evidence Pack must show the
verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from verirun.config import ModelPrice
from verirun.engine.models import Observation
from verirun.engine.structured import StructuredOutputError, complete_structured
from verirun.india.extraction import (
    AMOUNT_FIELDS,
    EXTRACTION_SYSTEM_PROMPT,
    FIELD_ORDER,
    IndiaInvoiceFields,
    citation_problems,
    extraction_prompt,
    low_confidence_fields,
    min_confidence,
    normalized_fields,
    parsed_invoice,
    raw_fields,
    validate_fields,
)
from verirun.llm.client import AssistantTurn, LLMClient, Message
from verirun.llm.meter import meter_turns
from verirun.tools.base import ToolError, require_str
from verirun.tools.files import FileTool, read_pdf_text

SCHEMA_NAME = "india-gst"


def india_extraction_messages(document_text: str) -> list[Message]:
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{extraction_prompt()}\nInvoice text:\n{document_text}",
        },
    ]


def extract_india_fields(
    client: LLMClient,
    document_text: str,
    on_turn: Callable[[AssistantTurn], None] | None = None,
) -> IndiaInvoiceFields:
    """Ask the model for the GST fields under the strict schema."""
    return complete_structured(
        client,
        india_extraction_messages(document_text),
        IndiaInvoiceFields,
        model_role="reason",
        on_turn=on_turn,
    )


class ExtractIndiaInvoiceTool(FileTool):
    """Extract India GST fields, citations, confidences, and validations."""

    name = "invoice.extract"
    description = (
        "Extract India GST invoice fields from a text-layer PDF: supplier, GSTIN, "
        "invoice number and dates, taxable amount, CGST, SGST, IGST, total, "
        "HSN/SAC, PO reference, and INR. Returns every field with a confidence "
        "and a citation, plus GSTIN checksum and tax-sum validations. Read a "
        "scanned PDF with files.extract instead."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the invoice PDF, relative to the shared root.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        client: LLMClient,
        *,
        confidence_threshold: float,
        prices: dict[str, ModelPrice] | None = None,
    ) -> None:
        super().__init__(root)
        self.client = client
        self.confidence_threshold = confidence_threshold
        self.prices = dict(prices or {})

    def run(self, args: dict[str, Any]) -> Observation:
        relative = require_str(args, "path")
        path = self.existing(relative, kind="file")
        if path.suffix.lower() != ".pdf":
            raise ToolError("invalid", f"'{relative}' is not a PDF")
        text, pages = read_pdf_text(path)
        if not text.strip():
            raise ToolError(
                "invalid",
                f"'{relative}' has no text layer; read the scan with files.extract",
            )
        turns: list[AssistantTurn] = []
        base_data = {
            "path": self.relative(path),
            "method": "text",
            "schema": SCHEMA_NAME,
            "pages": pages,
            "threshold": self.confidence_threshold,
        }
        try:
            fields = extract_india_fields(self.client, text, on_turn=turns.append)
        except StructuredOutputError as exc:
            meter = meter_turns(self.prices, turns)
            raise ToolError(
                "invalid",
                f"the model could not extract '{relative}': {exc}",
                data={
                    **base_data,
                    "usage": meter.snapshot(),
                    "cost_usd": meter.total_usd,
                },
            ) from exc
        meter = meter_turns(self.prices, turns)
        raw = raw_fields(fields)
        try:
            normalized = normalized_fields(fields)
        except ToolError as exc:
            raise ToolError(
                exc.kind,
                exc.message,
                data={
                    **base_data,
                    "fields": raw,
                    "usage": meter.snapshot(),
                    "cost_usd": meter.total_usd,
                    **exc.data,
                },
            ) from exc
        validations = [result.as_dict() for result in validate_fields(normalized)]
        data = {
            **base_data,
            "fields": normalized,
            "parsed": parsed_invoice(normalized),
            "validations": validations,
            "min_confidence": min_confidence(fields),
            "usage": meter.snapshot(),
            "cost_usd": meter.total_usd,
        }
        problems = citation_problems(fields, text)
        if problems:
            details = "; ".join(f"{name}: {problem}" for name, problem in problems)
            raise ToolError(
                "invalid",
                f"the extraction for '{relative}' cites text that is not in the "
                f"document: {details}",
                data={**data, "citation_problems": problems},
            )
        low = low_confidence_fields(fields, self.confidence_threshold)
        if low:
            details = ", ".join(
                f"{name} ({raw[name]['confidence']:.2f})" for name in low
            )
            raise ToolError(
                "invalid",
                "the extraction has fields below the confidence threshold "
                f"{self.confidence_threshold:.2f}: {details}",
                data={**data, "low_confidence": True, "low_confidence_fields": low},
            )
        extracted = sum(
            1
            for name in FIELD_ORDER
            if _value(normalized, name) is not None
        )
        failed = [item for item in validations if not item["ok"]]
        summary = (
            f"Extracted {extracted} field(s) from '{self.relative(path)}' with "
            f"citations (lowest confidence {data['min_confidence']:.2f})"
        )
        if failed:
            summary += f"; {len(failed)} GST validation(s) failed"
        return Observation(ok=True, summary=summary, data=data)


def _value(normalized: dict[str, dict[str, Any]], name: str) -> Any:
    return normalized[AMOUNT_FIELDS.get(name, name)]["value"]
