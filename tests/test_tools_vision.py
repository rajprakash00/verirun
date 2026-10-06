"""Unit tests for the vision path: scanned PDFs, strict schema, confidence.

The vision model is scripted, so these tests are deterministic and offline
while the rendering touches a real image-only PDF from the seeded Mock Suite.
"""

from __future__ import annotations

import json

from tests.support import SCAN_PATH, ScriptedClient, vision_fields
from verirun.tools.vision import ExtractInvoiceTool

SCAN = SCAN_PATH
TEXT_INVOICE = "documents/invoices/NW-2026-001.pdf"


def tool(maildesk_state, script: ScriptedClient, threshold: float = 0.8) -> ExtractInvoiceTool:
    return ExtractInvoiceTool(
        maildesk_state.shared_root, script, confidence_threshold=threshold
    )


def test_a_scanned_invoice_is_extracted_with_per_field_confidence(maildesk_state) -> None:
    script = ScriptedClient([vision_fields()])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert observation.ok, observation.summary
    assert observation.data["path"] == SCAN
    assert observation.data["method"] == "vision"
    assert observation.data["pages"] == 1
    assert observation.data["threshold"] == 0.8
    fields = observation.data["fields"]
    assert fields["vendor_name"]["value"] == "Paperline Print Shop"
    assert fields["invoice_number"]["value"] == "PP-2026-042"
    assert fields["amount_cents"]["value"] == 105_000
    assert fields["currency"]["value"] == "USD"
    assert fields["purchase_order"]["value"] == "PO-2008"
    assert fields["goods_receipt"]["value"] == "GR-2508"
    assert fields["invoice_number"]["confidence"] == 0.99
    assert observation.data["min_confidence"] == 0.93


def test_the_vision_model_receives_the_rendered_page_and_the_vision_role(maildesk_state) -> None:
    script = ScriptedClient([vision_fields()])

    tool(maildesk_state, script).invoke({"path": SCAN})

    assert len(script.calls) == 1
    call = script.calls[0]
    assert call["model_role"] == "vision"
    assert call["response_format"] == {"type": "json_object"}
    user = call["messages"][1]
    assert user["role"] == "user"
    text_part, image_part = user["content"][0], user["content"][1]
    assert text_part["type"] == "text"
    assert "schema" in text_part["text"].lower()
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_fields_below_the_threshold_fail_with_the_reason(maildesk_state) -> None:
    script = ScriptedClient([vision_fields(amount=0.42)])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert observation.data["low_confidence"] is True
    assert observation.data["low_confidence_fields"] == ["amount"]
    assert "amount" in observation.summary
    assert "0.42" in observation.summary
    fields = observation.data["fields"]
    assert fields["amount_cents"]["confidence"] == 0.42
    assert fields["amount_cents"]["value"] == 105_000


def test_a_text_layer_pdf_is_refused_without_calling_the_model(maildesk_state) -> None:
    script = ScriptedClient([vision_fields()])

    observation = tool(maildesk_state, script).invoke({"path": TEXT_INVOICE})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "text layer" in observation.summary
    assert script.calls == []


def test_an_unreadable_vision_reply_fails_after_one_repair(maildesk_state) -> None:
    script = ScriptedClient(["not json at all", "still not json"])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "structured output" in observation.summary
    assert len(script.calls) == 2


def test_a_missing_required_field_is_reported_with_its_confidence(maildesk_state) -> None:
    reply = json.loads(vision_fields())
    reply["invoice_number"] = {"value": None, "confidence": 0.2}
    script = ScriptedClient([json.dumps(reply)])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert not observation.ok
    assert observation.error_kind == "invalid"
    assert "invoice_number" in observation.summary
    assert observation.data["fields"]["invoice_number"]["confidence"] == 0.2


def test_a_confident_absent_reference_is_null_not_an_error(maildesk_state) -> None:
    reply = json.loads(vision_fields())
    reply["purchase_order"] = {"value": "none", "confidence": 0.9}
    script = ScriptedClient([json.dumps(reply)])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert observation.ok, observation.summary
    assert observation.data["fields"]["purchase_order"]["value"] is None
    assert observation.data["min_confidence"] == 0.9


def test_an_unsure_absent_reference_still_fails_the_threshold(maildesk_state) -> None:
    reply = json.loads(vision_fields())
    reply["purchase_order"] = {"value": "none", "confidence": 0.4}
    script = ScriptedClient([json.dumps(reply)])

    observation = tool(maildesk_state, script).invoke({"path": SCAN})

    assert not observation.ok
    assert observation.data["low_confidence"] is True
    assert observation.data["low_confidence_fields"] == ["purchase_order"]
