"""Shared test helpers. Not a test module."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from company_operator.llm.client import AssistantTurn, ToolCall, Usage

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def serve(app: Any) -> Iterator[str]:
    """Serve an ASGI app on a free port for real browser tests."""
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"server did not start on {base_url}")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)

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
    """A deterministic LLMClient for tests.

    Replies may be plain text or ready-made ``AssistantTurn`` objects, so a
    script can drive the Execute tool loop with tool calls and usage.
    """

    def __init__(self, replies: list[str | AssistantTurn]) -> None:
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
        reply = self.replies.pop(0)
        if isinstance(reply, AssistantTurn):
            return reply
        return AssistantTurn(model="scripted", text=reply, usage=Usage())


def text_turn(text: str, *, usage: Usage | None = None) -> AssistantTurn:
    return AssistantTurn(model="scripted", text=text, usage=usage or Usage())


def tool_turn(
    name: str,
    arguments: dict | None = None,
    *,
    call_id: str = "call-1",
    usage: Usage | None = None,
) -> AssistantTurn:
    return AssistantTurn(
        model="scripted",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments or {})],
        usage=usage or Usage(),
    )


def work_order_json(**overrides) -> str:
    return json.dumps({**WORK_ORDER, **overrides})


def plan_json(**overrides) -> str:
    return json.dumps({**PLAN, **overrides})
