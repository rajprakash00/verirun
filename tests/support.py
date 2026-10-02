"""Shared test helpers. Not a test module."""

from __future__ import annotations

import json
from pathlib import Path

from company_operator.llm.client import AssistantTurn, Usage

ROOT = Path(__file__).resolve().parents[1]

WORK_ORDER = {
    "sop": "invoice-processing",
    "goal": "File the September invoices from the AP mailbox",
    "assumptions": ["Only the September batch is in scope"],
    "systems": ["maildesk", "ledgerlite"],
    "policies": ["spend-limits", "action-rules"],
    "approval_gates": [
        {
            "id": "over-spend-limit",
            "description": "Payments above 10,000 USD wait for a human",
            "policy": "spend-limits",
        }
    ],
    "success_criteria": ["Invoice NW-2026-001 exists in LedgerLite"],
    "open_questions": [],
}

PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and attachment",
            "allowed_tools": ["mail.read", "files.write"],
            "done_criterion": "The invoice fields are extracted from the PDF",
        },
        {
            "id": "step-2",
            "goal": "Match the invoice against its purchase order and goods receipt",
            "allowed_tools": ["erp.get_purchase_order", "erp.get_goods_receipt"],
            "done_criterion": "The amounts agree and no duplicate exists",
        },
        {
            "id": "step-3",
            "goal": "File the invoice and prepare the payment",
            "allowed_tools": ["erp.file_invoice", "erp.schedule_payment"],
            "done_criterion": "The invoice exists in LedgerLite and the payment is prepared",
        },
    ]
}


class ScriptedClient:
    """A deterministic LLMClient for tests."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(
        self,
        messages,
        tools=None,
        model_role="loop",
        response_format=None,
    ) -> AssistantTurn:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model_role": model_role,
                "response_format": response_format,
            }
        )
        if not self.replies:
            raise AssertionError("scripted client ran out of replies")
        return AssistantTurn(model="scripted", text=self.replies.pop(0), usage=Usage())


def work_order_json(**overrides) -> str:
    return json.dumps({**WORK_ORDER, **overrides})


def plan_json(**overrides) -> str:
    return json.dumps({**PLAN, **overrides})
