"""The seeded eval scenarios: one fixed Run per Mock Suite scenario.

Each scenario carries a scripted LLM client, so the eval is deterministic and
offline. The harness records the client's turns into replay fixtures once; every
later run replays them. The scenarios assert the known terminal outcomes of the
seeded Mock Suite, including the browser re-grounding scenario.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from verirun.engine.states import RunState
from verirun.llm.client import AssistantTurn, LLMClient, Message, ToolCall, Usage

MODEL_FOR_ROLE = {
    "loop": "deepseek-v4.1-flash",
    "reason": "deepseek-v4-pro",
    "vision": "deepseek-v4-flash-vision-exp",
}

TURN_USAGE = Usage(prompt_tokens=1500, completion_tokens=60)
VISION_USAGE = Usage(prompt_tokens=1200, completion_tokens=120)

FILED_INVOICE_ID = "INV-3003"
SCHEDULED_FOR = "2026-09-30"
SCAN_PATH = "documents/invoices/PP-2026-042.pdf"
CASCADE_FORM = "documents/vendor-onboarding/cascade-fabrication-w9.pdf"

LEDGERLITE_URL = "http://127.0.0.1:8002"

VISION_FIELDS = {
    "vendor_name": {"value": "Paperline Print Shop", "confidence": 0.97},
    "invoice_number": {"value": "PP-2026-042", "confidence": 0.99},
    "amount": {"value": "1,050.00", "confidence": 0.95},
    "currency": {"value": "USD", "confidence": 0.98},
    "purchase_order": {"value": "PO-2008", "confidence": 0.94},
    "goods_receipt": {"value": "GR-2508", "confidence": 0.93},
}


def vision_turn() -> AssistantTurn:
    """The strict-schema JSON a scripted vision model replies with."""
    return AssistantTurn(
        model=MODEL_FOR_ROLE["vision"],
        text=json.dumps(VISION_FIELDS),
        usage=VISION_USAGE,
    )


def text_turn(text: str) -> AssistantTurn:
    return AssistantTurn(model=MODEL_FOR_ROLE["loop"], text=text, usage=TURN_USAGE)


def tool_turn(
    name: str, arguments: dict[str, Any] | None = None, *, call_id: str = "call-1"
) -> AssistantTurn:
    return AssistantTurn(
        model=MODEL_FOR_ROLE["loop"],
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments or {})],
        usage=TURN_USAGE,
    )


def file_invoice_turn(
    number: str,
    vendor_id: str,
    amount: float,
    po_id: str,
    gr_id: str,
    *,
    call_id: str = "call-1",
) -> AssistantTurn:
    return tool_turn(
        "erp.file_invoice",
        {
            "number": number,
            "vendor_id": vendor_id,
            "amount": amount,
            "po_id": po_id,
            "gr_id": gr_id,
        },
        call_id=call_id,
    )


def schedule_payment_turn(*, call_id: str = "call-1") -> AssistantTurn:
    return tool_turn(
        "erp.schedule_payment",
        {"invoice_id": FILED_INVOICE_ID, "scheduled_for": SCHEDULED_FOR},
        call_id=call_id,
    )


def archive_turn(
    path: str, *, directory: str = "processed", call_id: str = "call-1"
) -> AssistantTurn:
    return tool_turn(
        "files.archive", {"path": path, "directory": directory}, call_id=call_id
    )


class StageClient:
    """A deterministic LLM client: string replies become turns for their role."""

    def __init__(self, replies: list[str | AssistantTurn]) -> None:
        self.replies = list(replies)

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model_role: str = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        if not self.replies:
            raise AssertionError("the scenario script ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, AssistantTurn):
            return reply
        return AssistantTurn(model=MODEL_FOR_ROLE[model_role], text=reply, usage=TURN_USAGE)


def find_ref(refs: dict[str, dict[str, Any]], *, role: str, name: str) -> str:
    """The ref of the first node matching a role and a partial accessible name."""
    for ref, node in refs.items():
        if node["role"] == role and name.lower() in node["name"].lower():
            return ref
    raise AssertionError(f"no {role} named {name!r} in the snapshot")


def _latest_refs(messages: list[Message]) -> dict[str, dict[str, Any]]:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        payload = json.loads(message["content"])
        refs = (payload.get("data") or {}).get("refs")
        if refs:
            return refs
    raise AssertionError("no snapshot observation in the engine messages")


class BrowserStage:
    """Drives the ui_changed scenario: static work order and plan, then browser
    turns that read the snapshot refs out of the engine messages."""

    def __init__(self, work_order: str, plan: str, url: str) -> None:
        self.work_order = work_order
        self.plan = plan
        self.url = url
        self.reason_turns = 0
        self.loop_turn = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model_role: str = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        if model_role == "reason":
            self.reason_turns += 1
            text = self.work_order if self.reason_turns == 1 else self.plan
            return AssistantTurn(model=MODEL_FOR_ROLE["reason"], text=text, usage=TURN_USAGE)
        turn = self.loop_turn
        self.loop_turn += 1
        if turn == 0:
            return tool_turn("browser.navigate", {"url": self.url}, call_id="c1")
        if turn == 1:
            return tool_turn("browser.snapshot", call_id="c2")
        if turn in (2, 3):
            refs = _latest_refs(messages)
            ref = find_ref(refs, role="textbox", name="Vendor invoice number")
            if turn == 2:
                return tool_turn(
                    "browser.type", {"ref": ref, "text": "NW-2026-001"}, call_id="c3"
                )
            return tool_turn("browser.extract", {"ref": ref}, call_id="c4")
        if turn == 4:
            return text_turn("The form shows NW-2026-001 in the invoice number field.")
        if turn == 5:
            return tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c5")
        if turn == 6:
            return tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, call_id="c6")
        if turn == 7:
            return text_turn("PO-2001 and GR-2501 agree with the invoice amount.")
        if turn == 8:
            return file_invoice_turn(
                "NW-2026-001", "V-1001", 1250.00, "PO-2001", "GR-2501", call_id="c7"
            )
        if turn == 9:
            return text_turn(f"Filed as {FILED_INVOICE_ID}.")
        if turn == 10:
            return schedule_payment_turn(call_id="c8")
        if turn == 11:
            return archive_turn("documents/invoices/NW-2026-001.pdf", call_id="c9")
        return text_turn("Payment scheduled and the source archived.")


def invoice_work_order(goal: str, criteria: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "sop": "invoice-processing",
        "goal": goal,
        "assumptions": ["Only the September requests in scope are handled"],
        "systems": ["maildesk", "ledgerlite", "files"],
        "policies": ["spend-limits", "action-rules"],
        "approval_gates": [
            {
                "id": "over-spend-limit",
                "description": "Payments above 10,000 USD wait for a human",
                "policy": "spend-limits",
            }
        ],
        "success_criteria": criteria,
        "open_questions": [],
        **overrides,
    }


def vendor_work_order(goal: str, criteria: list[str]) -> dict[str, Any]:
    return {
        "sop": "vendor-onboarding",
        "goal": goal,
        "assumptions": ["The procurement mailbox holds the vendor request in scope"],
        "systems": ["maildesk", "ledgerlite", "files"],
        "policies": ["vendor-management"],
        "approval_gates": [
            {
                "id": "vendor-creation",
                "description": "Creating a vendor waits for a human decision",
                "policy": "vendor-management",
            }
        ],
        "success_criteria": criteria,
        "open_questions": [],
    }


HAPPY_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the invoice email and locate its attachment",
            "allowed_tools": ["mail.read", "files.read"],
            "done_criterion": "The invoice NW-2026-001 is located in the shared file tree",
        },
        {
            "id": "step-2",
            "goal": "Validate and file the invoice in LedgerLite",
            "allowed_tools": [
                "erp.get_purchase_order",
                "erp.get_goods_receipt",
                "erp.file_invoice",
            ],
            "done_criterion": "NW-2026-001 is filed against PO-2001 and GR-2501",
        },
        {
            "id": "step-3",
            "goal": "Schedule the payment and archive the source",
            "allowed_tools": ["erp.schedule_payment", "files.archive"],
            "done_criterion": "The payment is scheduled and the source is in processed",
        },
    ]
}

TRANSIENT_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Validate the Riverstone invoice against its references",
            "allowed_tools": ["erp.get_purchase_order", "erp.get_goods_receipt"],
            "done_criterion": "The amount agrees with the PO and goods receipt",
        },
        {
            "id": "step-2",
            "goal": "File the Riverstone invoice and schedule payment",
            "allowed_tools": ["erp.file_invoice", "erp.schedule_payment"],
            "done_criterion": "RS-2026-088 is filed and the payment scheduled",
        },
        {
            "id": "step-3",
            "goal": "Archive the source document",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}

SCAN_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the Paperline scan and extract its fields",
            "allowed_tools": ["mail.read", "files.extract"],
            "done_criterion": "The invoice fields are extracted with confidence",
        },
        {
            "id": "step-2",
            "goal": "Validate and file the invoice in LedgerLite",
            "allowed_tools": [
                "erp.list_vendors",
                "erp.get_purchase_order",
                "erp.get_goods_receipt",
                "erp.file_invoice",
            ],
            "done_criterion": "PP-2026-042 is filed against PO-2008 and GR-2508",
        },
        {
            "id": "step-3",
            "goal": "Schedule the payment and archive the source",
            "allowed_tools": ["erp.schedule_payment", "files.archive"],
            "done_criterion": "The payment is scheduled and the source is in processed",
        },
    ]
}

OVER_LIMIT_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Summit Industrial invoice",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "SI-2026-550 is filed against PO-2005 and GR-2505",
        },
        {
            "id": "step-2",
            "goal": "Prepare and schedule the payment",
            "allowed_tools": ["erp.schedule_payment"],
            "done_criterion": "The payment is scheduled once a human approves it",
        },
        {
            "id": "step-3",
            "goal": "Archive the source invoice",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The source is in processed",
        },
    ]
}

DUPLICATE_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Apex invoice",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "AQ-2026-014 is filed",
        }
    ]
}

MISMATCH_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Cedar invoice",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "CD-2026-007 is filed",
        }
    ]
}

MISSING_PO_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Match the Bright Path invoice to a purchase order",
            "allowed_tools": ["erp.list_purchase_orders"],
            "done_criterion": "The matching purchase order is known",
        }
    ]
}

FORBIDDEN_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Blocked Supplies invoice",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "BL-2026-001 is filed",
        },
        {
            "id": "step-2",
            "goal": "Schedule the payment",
            "allowed_tools": ["erp.schedule_payment"],
            "done_criterion": "The payment is scheduled",
        },
    ]
}

UI_CHANGED_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Check the LedgerLite new-invoice form through the browser UI",
            "allowed_tools": [
                "browser.navigate",
                "browser.snapshot",
                "browser.type",
                "browser.extract",
            ],
            "done_criterion": "The form accepts NW-2026-001 despite the UI change",
        },
        {
            "id": "step-2",
            "goal": "Match the invoice against its purchase order and goods receipt",
            "allowed_tools": ["erp.get_purchase_order", "erp.get_goods_receipt"],
            "done_criterion": "The amounts agree",
        },
        {
            "id": "step-3",
            "goal": "File the invoice in LedgerLite",
            "allowed_tools": ["erp.file_invoice"],
            "done_criterion": "NW-2026-001 is filed against PO-2001 and GR-2501",
        },
        {
            "id": "step-4",
            "goal": "Schedule the payment and archive the source",
            "allowed_tools": ["erp.schedule_payment", "files.archive"],
            "done_criterion": "The payment is scheduled and the source is in processed",
        },
    ]
}

VENDOR_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "Read the onboarding email and its tax form",
            "allowed_tools": ["mail.read", "files.read"],
            "done_criterion": "The vendor legal name, tax id, and email are extracted",
        },
        {
            "id": "step-2",
            "goal": "Check LedgerLite for an existing vendor",
            "allowed_tools": ["erp.list_vendors"],
            "done_criterion": "No vendor with the same name or tax id exists",
        },
        {
            "id": "step-3",
            "goal": "Create the vendor in LedgerLite",
            "allowed_tools": ["erp.create_vendor"],
            "done_criterion": "The vendor exists in LedgerLite",
        },
        {
            "id": "step-4",
            "goal": "Archive the tax form",
            "allowed_tools": ["files.archive"],
            "done_criterion": "The tax form is in the archive folder",
        },
    ]
}

CASCADE = {
    "name": "Cascade Fabrication LLC",
    "tax_id": "TAX-2001",
    "address": "4820 Foundry Way, Portland, OR 97210",
    "email": "accounts@cascade-fabrication.example",
}


def happy_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the September invoices from the AP mailbox",
                    ["Invoice NW-2026-001 exists in LedgerLite"],
                )
            ),
            json.dumps(HAPPY_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7001"}, call_id="c1"),
            text_turn("Located NW-2026-001 in the batch email."),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, call_id="c2"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, call_id="c3"),
            file_invoice_turn(
                "NW-2026-001", "V-1001", 1250.00, "PO-2001", "GR-2501", call_id="c4"
            ),
            text_turn(f"Filed as {FILED_INVOICE_ID}."),
            schedule_payment_turn(call_id="c5"),
            archive_turn("documents/invoices/NW-2026-001.pdf", call_id="c6"),
            text_turn("Payment scheduled and the source archived."),
        ]
    )


def transient_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the Riverstone Logistics invoice from the September batch",
                    ["Invoice RS-2026-088 exists in LedgerLite"],
                )
            ),
            json.dumps(TRANSIENT_PLAN),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2007"}, call_id="c1"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2507"}, call_id="c2"),
            text_turn("PO and goods receipt agree."),
            file_invoice_turn(
                "RS-2026-088", "V-1007", 890.00, "PO-2007", "GR-2507", call_id="c3"
            ),
            schedule_payment_turn(call_id="c4"),
            text_turn("Filed and scheduled."),
            archive_turn("documents/invoices/RS-2026-088.pdf", call_id="c5"),
            text_turn("Archived."),
        ]
    )


def scanned_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the scanned Paperline Print Shop invoice",
                    ["Invoice PP-2026-042 exists in LedgerLite"],
                )
            ),
            json.dumps(SCAN_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7008"}, call_id="c1"),
            tool_turn("files.extract", {"path": SCAN_PATH}, call_id="c2"),
            vision_turn(),
            text_turn("Extracted PP-2026-042 from the scan."),
            tool_turn("erp.list_vendors", {}, call_id="c3"),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2008"}, call_id="c4"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2508"}, call_id="c5"),
            file_invoice_turn(
                "PP-2026-042", "V-1008", 1050.00, "PO-2008", "GR-2508", call_id="c6"
            ),
            text_turn(f"Filed as {FILED_INVOICE_ID}."),
            schedule_payment_turn(call_id="c7"),
            archive_turn(SCAN_PATH, call_id="c8"),
            text_turn("Payment scheduled and source archived."),
            # The Verifier re-reads the scan itself, independently of the Run.
            vision_turn(),
        ]
    )


def over_limit_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the Summit Industrial invoice and prepare its payment",
                    ["Invoice SI-2026-550 exists in LedgerLite"],
                )
            ),
            json.dumps(OVER_LIMIT_PLAN),
            file_invoice_turn(
                "SI-2026-550", "V-1005", 12500.00, "PO-2005", "GR-2505", call_id="c1"
            ),
            text_turn("Filed SI-2026-550."),
            schedule_payment_turn(call_id="c2"),
            # Consumed after the human approval resumes the Run.
            text_turn("Payment scheduled."),
            archive_turn("documents/invoices/SI-2026-550.pdf", call_id="c3"),
            text_turn("Archived."),
        ]
    )


def duplicate_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the Apex Office Supplies invoice",
                    ["Invoice AQ-2026-014 is not filed twice"],
                )
            ),
            json.dumps(DUPLICATE_PLAN),
            file_invoice_turn(
                "AQ-2026-014", "V-1002", 640.00, "PO-2002", "GR-2502", call_id="c1"
            ),
        ]
    )


def amount_mismatch_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the Cedar Components invoice",
                    ["The invoice amount is agreed with the purchase order"],
                )
            ),
            json.dumps(MISMATCH_PLAN),
            file_invoice_turn(
                "CD-2026-007", "V-1003", 2100.00, "PO-2003", "GR-2503", call_id="c1"
            ),
        ]
    )


def missing_po_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "Match the Bright Path Consulting invoice to a purchase order",
                    ["The purchase order covering BP-2026-123 is known"],
                )
            ),
            json.dumps(MISSING_PO_PLAN),
            tool_turn("erp.list_purchase_orders", {"vendor_id": "V-1004"}, call_id="c1"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "Invoice BP-2026-123 has no purchase order in LedgerLite",
                    "question": "Which purchase order covers BP-2026-123?",
                },
                call_id="c2",
            ),
        ]
    )


def forbidden_action_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                invoice_work_order(
                    "File the Blocked Supplies invoice and schedule its payment",
                    ["The blocked vendor is never paid"],
                )
            ),
            json.dumps(FORBIDDEN_PLAN),
            file_invoice_turn(
                "BL-2026-001", "V-1006", 300.00, "PO-2006", "GR-2506", call_id="c1"
            ),
            text_turn("BL-2026-001 filed."),
            schedule_payment_turn(call_id="c2"),
        ]
    )


def ui_changed_script() -> LLMClient:
    return BrowserStage(
        json.dumps(
            invoice_work_order(
                "File the Northwind invoice after checking the LedgerLite form",
                ["Invoice NW-2026-001 exists in LedgerLite"],
            )
        ),
        json.dumps(UI_CHANGED_PLAN),
        f"{LEDGERLITE_URL}/invoices/new?ui=changed",
    )


def vendor_onboarding_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                vendor_work_order(
                    "Onboard Cascade Fabrication LLC as a new supplier",
                    ["Cascade Fabrication LLC exists in LedgerLite exactly once"],
                )
            ),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7009"}, call_id="c1"),
            text_turn("Read the W-9 attachment for Cascade Fabrication LLC."),
            tool_turn("erp.list_vendors", {}, call_id="c2"),
            text_turn("No existing vendor matches Cascade Fabrication LLC."),
            tool_turn("erp.create_vendor", CASCADE, call_id="c3"),
            # Consumed after the human approval resumes the Run.
            text_turn("Vendor created."),
            archive_turn(CASCADE_FORM, directory="archive", call_id="c4"),
            text_turn("Tax form archived."),
        ]
    )


def vendor_duplicate_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                vendor_work_order(
                    "Onboard Apex Office Supplies as a new supplier",
                    ["Apex Office Supplies is not created twice"],
                )
            ),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7010"}, call_id="c1"),
            tool_turn(
                "files.read",
                {"path": "documents/vendor-onboarding/apex-office-supplies-w9.pdf"},
                call_id="c2",
            ),
            text_turn("Extracted Apex Office Supplies with tax id TAX-1002."),
            tool_turn("erp.list_vendors", {}, call_id="c3"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "A vendor with tax id TAX-1002 already exists as V-1002",
                    "question": (
                        "Apex Office Supplies is already vendor V-1002. Should the Run "
                        "update the existing vendor instead of creating a duplicate?"
                    ),
                },
                call_id="c4",
            ),
        ]
    )


def vendor_missing_tax_form_script() -> StageClient:
    return StageClient(
        [
            json.dumps(
                vendor_work_order(
                    "Onboard Meridian Plastics as a new supplier",
                    ["Meridian Plastics is onboarded with a tax form"],
                )
            ),
            json.dumps(VENDOR_PLAN),
            tool_turn("mail.read", {"message_id": "MSG-7011"}, call_id="c1"),
            tool_turn(
                "task.escalate",
                {
                    "reason": "The onboarding email for Meridian Plastics has no tax form",
                    "question": "Which tax form should be used for Meridian Plastics?",
                },
                call_id="c2",
            ),
        ]
    )


@dataclass(frozen=True)
class Scenario:
    """One seeded scenario the eval runs end to end."""

    key: str
    title: str
    task_id: str
    request: str
    script: Callable[[], LLMClient]
    expected_state: RunState
    parked_state: RunState | None = None
    approval: bool = False
    checks: tuple[str, ...] = ()
    escalations: int = 0
    approvals: int = 0
    resumed: bool = False
    regrounded: bool = False
    retried_tool: str | None = None
    escalation_reason: str | None = None
    needs_browser: bool = False

    @property
    def run_id(self) -> str:
        return "EVAL-" + self.key.upper().replace("_", "-")


INVOICE_CHECKS = ("invoice-filed", "payment-scheduled", "source-archived")
VENDOR_CHECKS = ("vendor-created", "tax-document-archived")

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="happy",
        title="Invoice happy path",
        task_id="invoice-processing",
        request="Process the September invoice batch in the AP mailbox",
        script=happy_script,
        expected_state=RunState.COMPLETED,
        checks=INVOICE_CHECKS,
    ),
    Scenario(
        key="transient",
        title="Transient ERP failure",
        task_id="invoice-processing",
        request="Process the Riverstone invoice from the AP mailbox",
        script=transient_script,
        expected_state=RunState.COMPLETED,
        checks=INVOICE_CHECKS,
        retried_tool="erp.file_invoice",
    ),
    Scenario(
        key="scanned",
        title="Scanned invoice via vision",
        task_id="invoice-processing",
        request="Process the scanned Paperline invoice from the AP mailbox",
        script=scanned_script,
        expected_state=RunState.COMPLETED,
        checks=INVOICE_CHECKS,
    ),
    Scenario(
        key="over_limit",
        title="Over-limit payment behind approval",
        task_id="invoice-processing",
        request="Process the Summit Industrial invoice from the AP mailbox",
        script=over_limit_script,
        expected_state=RunState.COMPLETED,
        parked_state=RunState.AWAITING_APPROVAL,
        approval=True,
        approvals=1,
        resumed=True,
        checks=INVOICE_CHECKS,
    ),
    Scenario(
        key="duplicate",
        title="Duplicate invoice escalation",
        task_id="invoice-processing",
        request="Process the Apex Office Supplies invoice from the AP mailbox",
        script=duplicate_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
    ),
    Scenario(
        key="amount_mismatch",
        title="Amount mismatch escalation",
        task_id="invoice-processing",
        request="Process the Cedar Components invoice from the AP mailbox",
        script=amount_mismatch_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
    ),
    Scenario(
        key="missing_po",
        title="Missing purchase order escalation",
        task_id="invoice-processing",
        request="Process the Bright Path Consulting invoice from the AP mailbox",
        script=missing_po_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
    ),
    Scenario(
        key="forbidden_action",
        title="Forbidden payment escalation",
        task_id="invoice-processing",
        request="Process the Blocked Supplies invoice from the AP mailbox",
        script=forbidden_action_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
        escalation_reason="policy denial",
    ),
    Scenario(
        key="ui_changed",
        title="Browser re-grounding after a UI change",
        task_id="invoice-processing",
        request="Process the Northwind invoice through the LedgerLite browser UI",
        script=ui_changed_script,
        expected_state=RunState.COMPLETED,
        checks=INVOICE_CHECKS,
        regrounded=True,
        needs_browser=True,
    ),
    Scenario(
        key="vendor_onboarding",
        title="Vendor onboarding behind approval",
        task_id="vendor-onboarding",
        request="Onboard Cascade Fabrication LLC from the procurement mailbox",
        script=vendor_onboarding_script,
        expected_state=RunState.COMPLETED,
        parked_state=RunState.AWAITING_APPROVAL,
        approval=True,
        approvals=1,
        resumed=True,
        checks=VENDOR_CHECKS,
    ),
    Scenario(
        key="vendor_duplicate",
        title="Duplicate vendor escalation",
        task_id="vendor-onboarding",
        request="Onboard Apex Office Supplies from the procurement mailbox",
        script=vendor_duplicate_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
    ),
    Scenario(
        key="vendor_missing_tax_form",
        title="Missing tax form escalation",
        task_id="vendor-onboarding",
        request="Onboard Meridian Plastics from the procurement mailbox",
        script=vendor_missing_tax_form_script,
        expected_state=RunState.NEEDS_HUMAN,
        escalations=1,
    ),
)
