"""The vision path for scanned invoices.

A PDF that carries a text layer is read with ``files.read``. A scan has no text
layer, so ``files.extract`` renders its pages to images and asks the vision
model to read the invoice fields under a strict JSON schema, each field with a
confidence. The Task Pack's ``extraction.confidence_threshold`` decides whether
the extraction is trusted: any field below it fails the call with
``low_confidence``, and the engine escalates to a human with the reason.

The Verifier reads scans with the same schema, but from its own vision call on
the source file. It never trusts the executor's extraction.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pypdfium2 as pdfium
from pydantic import BaseModel, ConfigDict, Field

from verirun.config import ModelPrice
from verirun.engine.models import Observation
from verirun.engine.structured import StructuredOutputError, complete_structured
from verirun.llm.client import AssistantTurn, LLMClient, Message
from verirun.llm.meter import meter_turns
from verirun.tools.base import ToolError, require_str
from verirun.tools.erp import parse_amount
from verirun.tools.files import FileTool, read_pdf_text

RENDER_SCALE = 2

FIELD_ORDER = (
    "vendor_name",
    "invoice_number",
    "amount",
    "currency",
    "purchase_order",
    "goods_receipt",
)
REQUIRED_FIELDS = ("vendor_name", "invoice_number", "amount", "currency")
OPTIONAL_FIELDS = ("purchase_order", "goods_receipt")
ABSENT_VALUES = {"none", "null", "n/a"}
NORMALIZED_KEYS = {"amount": "amount_cents"}

VISION_SYSTEM_PROMPT = """\
You are the document extraction step of Verirun. You read scanned invoices and
return their fields as JSON. You never guess: you report only what the image
shows, and you state how certain you are of every value.
"""


class ExtractedField(BaseModel):
    """One field read from the scan, with the model's confidence in it."""

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class InvoiceFields(BaseModel):
    """The strict schema the vision model must return."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: ExtractedField
    invoice_number: ExtractedField
    amount: ExtractedField
    currency: ExtractedField
    purchase_order: ExtractedField
    goods_receipt: ExtractedField


def extraction_prompt() -> str:
    schema = json.dumps(InvoiceFields.model_json_schema(), indent=2)
    return (
        "Read the invoice in the attached page image(s) and reply with one JSON "
        "object that follows this JSON schema exactly:\n"
        f"{schema}\n"
        "Rules:\n"
        "- Read only what is visible in the image. Never guess a value.\n"
        '- For every field give "confidence" between 0 and 1: how certain you '
        "are of the value.\n"
        "- Required fields: vendor_name, invoice_number, amount, currency. Use "
        "null for a value you cannot read at all.\n"
        '- "amount" is digits and a decimal point only, for example "1050.00", '
        "with no currency symbol.\n"
        '- "currency" is the ISO code printed on the invoice, for example "USD".\n'
        "- purchase_order and goods_receipt are the reference numbers printed on "
        "the invoice, or null when the invoice does not cite one.\n"
    )


def render_pdf_pages(path: Path, *, scale: int = RENDER_SCALE) -> list[bytes]:
    """Render every page of a PDF to PNG bytes, for a vision model to read."""
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise ToolError("invalid", f"cannot render PDF '{path.name}': {exc}") from exc
    try:
        images: list[bytes] = []
        for index in range(len(document)):
            bitmap = document[index].render(scale=scale)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG")
            images.append(buffer.getvalue())
        return images
    except Exception as exc:
        raise ToolError("invalid", f"cannot render PDF '{path.name}': {exc}") from exc
    finally:
        document.close()


def vision_messages(images: list[bytes]) -> list[Message]:
    content: list[dict[str, Any]] = [{"type": "text", "text": extraction_prompt()}]
    for image in images:
        encoded = base64.b64encode(image).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
        )
    return [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def extract_invoice_fields(
    client: LLMClient,
    images: list[bytes],
    on_turn: Callable[[AssistantTurn], None] | None = None,
) -> InvoiceFields:
    """Ask the vision model for the invoice fields under the strict schema."""
    return complete_structured(
        client,
        vision_messages(images),
        InvoiceFields,
        model_role="vision",
        on_turn=on_turn,
    )


def raw_fields(fields: InvoiceFields) -> dict[str, dict[str, Any]]:
    """The model's words and confidences, before any normalization."""
    return {
        name: {
            "value": getattr(fields, name).value,
            "confidence": getattr(fields, name).confidence,
        }
        for name in FIELD_ORDER
    }


def normalized_fields(fields: InvoiceFields) -> dict[str, dict[str, Any]]:
    """The extracted fields as the Run records them: typed values and confidences."""
    values = {name: (getattr(fields, name).value or "").strip() for name in FIELD_ORDER}
    missing = [name for name in REQUIRED_FIELDS if not values[name]]
    if missing:
        raise ToolError(
            "invalid",
            f"the vision model did not read required field(s): {', '.join(missing)}",
        )
    normalized: dict[str, dict[str, Any]] = {}
    for name in FIELD_ORDER:
        confidence = getattr(fields, name).confidence
        if name == "amount":
            normalized["amount_cents"] = {
                "value": _amount_cents(values[name]),
                "confidence": confidence,
            }
        elif name in OPTIONAL_FIELDS:
            normalized[name] = {"value": _reference(values[name]), "confidence": confidence}
        else:
            normalized[name] = {"value": values[name], "confidence": confidence}
    return normalized


def low_confidence_fields(fields: InvoiceFields, threshold: float) -> list[str]:
    """Every field, present or absent, the model is less sure of than the threshold."""
    return [
        name for name in FIELD_ORDER if getattr(fields, name).confidence < threshold
    ]


def min_confidence(fields: InvoiceFields) -> float:
    return min(getattr(fields, name).confidence for name in FIELD_ORDER)


def parsed_invoice(fields: InvoiceFields) -> dict[str, Any]:
    """The extracted fields reduced to what the Verifier compares with ground truth."""
    normalized = normalized_fields(fields)
    return {
        "vendor": normalized["vendor_name"]["value"],
        "number": normalized["invoice_number"]["value"],
        "amount_cents": normalized["amount_cents"]["value"],
        "currency": normalized["currency"]["value"],
    }


def read_scanned_invoice(
    client: LLMClient, path: Path, threshold: float
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    """The Verifier's own independent read of a scanned invoice.

    Renders the source file and asks the vision model directly, so the check
    never trusts the executor's extraction. Returns (parsed fields, lowest
    confidence, problem).
    """
    try:
        images = render_pdf_pages(path)
        if not images:
            return None, None, "the source document has no pages to render"
        fields = extract_invoice_fields(client, images)
    except (ToolError, StructuredOutputError) as exc:
        return None, None, f"the Verifier could not read the scan: {exc}"
    low = low_confidence_fields(fields, threshold)
    minimum = min_confidence(fields)
    if low:
        raw = raw_fields(fields)
        details = ", ".join(f"{name} ({raw[name]['confidence']:.2f})" for name in low)
        return None, minimum, (
            f"the Verifier's own extraction has fields below the confidence "
            f"threshold {threshold:.2f}: {details}"
        )
    try:
        return parsed_invoice(fields), minimum, None
    except ToolError as exc:
        return None, minimum, f"the Verifier's own extraction is incomplete: {exc.message}"


def _amount_cents(text: str) -> int:
    try:
        return parse_amount(text)
    except ToolError as exc:
        raise ToolError(
            "invalid",
            f"the vision model returned an amount that is not a number: {text!r}",
        ) from exc


def _reference(text: str) -> str | None:
    return None if not text or text.lower() in ABSENT_VALUES else text


class ExtractInvoiceTool(FileTool):
    """Extract invoice fields from an image-only PDF with the vision model."""

    name = "files.extract"
    description = (
        "Extract invoice fields from a scanned PDF that has no text layer, "
        "using the vision model. Returns every field with a confidence. Read a "
        "PDF that carries a text layer with files.read instead."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the scanned PDF, relative to the shared root.",
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
        text, _pages = read_pdf_text(path)
        if text.strip():
            raise ToolError("invalid", f"'{relative}' has a text layer; read it with files.read")
        images = render_pdf_pages(path)
        if not images:
            raise ToolError("invalid", f"'{relative}' has no pages to render")
        turns: list[AssistantTurn] = []
        try:
            fields = extract_invoice_fields(self.client, images, on_turn=turns.append)
        except StructuredOutputError as exc:
            raise ToolError(
                "invalid",
                f"the vision model could not extract '{relative}': {exc}",
                data=self._base_data(self.relative(path), images, turns),
            ) from exc
        base_data = self._base_data(self.relative(path), images, turns)
        raw = raw_fields(fields)
        try:
            normalized = normalized_fields(fields)
        except ToolError as exc:
            raise ToolError(
                exc.kind, exc.message, {**base_data, "fields": raw, **exc.data}
            ) from exc
        extracted = [
            name
            for name in FIELD_ORDER
            if normalized[NORMALIZED_KEYS.get(name, name)]["value"] is not None
        ]
        minimum = min_confidence(fields)
        data = {**base_data, "fields": normalized, "min_confidence": minimum}
        low = low_confidence_fields(fields, self.confidence_threshold)
        if low:
            details = ", ".join(f"{name} ({raw[name]['confidence']:.2f})" for name in low)
            raise ToolError(
                "invalid",
                "the scan has fields below the confidence threshold "
                f"{self.confidence_threshold:.2f}: {details}",
                data={**data, "low_confidence": True, "low_confidence_fields": low},
            )
        return Observation(
            ok=True,
            summary=(
                f"Extracted {len(extracted)} field(s) from '{self.relative(path)}' with "
                f"the vision model (lowest confidence {minimum:.2f})"
            ),
            data=data,
        )

    def _base_data(
        self, relative: str, images: list[bytes], turns: list[AssistantTurn]
    ) -> dict[str, Any]:
        meter = meter_turns(self.prices, turns)
        return {
            "path": relative,
            "method": "vision",
            "pages": len(images),
            "threshold": self.confidence_threshold,
            "usage": meter.snapshot(),
            "cost_usd": meter.total_usd,
        }
