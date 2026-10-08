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

from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.models import WorkOrder
from verirun.context.task_pack import load_task_pack
from verirun.engine.models import CheckResult, Observation, Plan
from verirun.engine.orchestrator import run_task
from verirun.engine.states import RunState
from verirun.llm.client import AssistantTurn, ToolCall, Usage
from verirun.runs.store import RunStore
from verirun.runtime import build_registry

ROOT = Path(__file__).resolve().parents[1]


def run_settings(tmp_path: Path, *, ledgerlite_db: Path, maildesk_state: Any) -> Settings:
    return Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=tmp_path / "runs" / "verirun.db",
        shared_dir=maildesk_state.shared_root,
        mail_db=maildesk_state.db_path,
        erp_db=ledgerlite_db,
    )


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

FILED_INVOICE_ID = "INV-3003"

SCAN_PATH = "documents/invoices/PP-2026-042.pdf"

VISION_FIELDS = {
    "vendor_name": {"value": "Paperline Print Shop", "confidence": 0.97},
    "invoice_number": {"value": "PP-2026-042", "confidence": 0.99},
    "amount": {"value": "1,050.00", "confidence": 0.95},
    "currency": {"value": "USD", "confidence": 0.98},
    "purchase_order": {"value": "PO-2008", "confidence": 0.94},
    "goods_receipt": {"value": "GR-2508", "confidence": 0.93},
}


def vision_fields(**confidences: float) -> str:
    """The strict-schema JSON a scripted vision model replies with."""
    fields = json.loads(json.dumps(VISION_FIELDS))
    for name, confidence in confidences.items():
        fields[name]["confidence"] = confidence
    return json.dumps(fields)


def vision_turn(**confidences: float) -> AssistantTurn:
    return AssistantTurn(
        model="deepseek-v4-flash-vision-exp",
        text=vision_fields(**confidences),
        usage=Usage(prompt_tokens=1200, completion_tokens=120),
    )

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


def location_turn(x: int, y: int, *, confidence: float = 0.9) -> AssistantTurn:
    """The strict-schema JSON the scripted vision model replies with."""
    return AssistantTurn(
        model="deepseek-v4-flash-vision-exp",
        text=json.dumps({"found": True, "x": x, "y": y, "confidence": confidence}),
        usage=Usage(prompt_tokens=1200, completion_tokens=60),
    )


def find_ref(refs: dict[str, dict], *, role: str, name: str) -> str:
    """The ref of the first node matching a role and a partial accessible name."""
    for ref, node in refs.items():
        if node["role"] == role and name.lower() in node["name"].lower():
            return ref
    raise AssertionError(f"no {role} named {name!r} in the snapshot")


def ref_for(snapshot: Observation, *, role: str, name: str) -> str:
    """The ref of the first snapshot node matching a role and a partial name."""
    try:
        return find_ref(snapshot.data["refs"], role=role, name=name)
    except AssertionError as exc:
        raise AssertionError(f"{exc}:\n{snapshot.data['snapshot']}") from exc


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


def park_over_limit_run(
    tmp_path: Path, ledgerlite_db: Path, maildesk_state: Any
) -> tuple[Settings, RunStore, ScriptedClient, str]:
    """Run the seeded over-limit invoice until it parks at the payment gate."""
    settings = run_settings(tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state)
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    store = RunStore(settings.run_db)
    script = over_limit_script()
    run = run_task(
        "Process the invoices in the AP mailbox",
        task_pack,
        context,
        script,
        store,
        build_registry(settings, context, task_pack, script),
        erp_db_path=ledgerlite_db,
        shared_root=maildesk_state.shared_root,
        evidence_root=settings.run_db.parent,
    )
    return settings, store, script, run.id


def over_limit_script() -> ScriptedClient:
    """Drive the over-limit invoice to the payment gate, then through approval."""
    return ScriptedClient(
        [
            json.dumps(WORK_ORDER),
            json.dumps(OVER_LIMIT_PLAN),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "SI-2026-550",
                    "vendor_id": "V-1005",
                    "amount": 12500.00,
                    "po_id": "PO-2005",
                    "gr_id": "GR-2505",
                },
                call_id="c1",
            ),
            text_turn("Filed SI-2026-550."),
            tool_turn(
                "erp.schedule_payment", {"invoice_id": FILED_INVOICE_ID}, call_id="c2"
            ),
            # Consumed after the human approval resumes the Run.
            text_turn("Payment scheduled."),
            tool_turn(
                "files.archive",
                {
                    "path": "documents/invoices/SI-2026-550.pdf",
                    "directory": "processed",
                },
                call_id="c3",
            ),
            text_turn("Archived."),
        ]
    )


def seed_completed_run(store: RunStore, run_id: str = "RUN-0001") -> None:
    """Persist a fully verified Run with observations, a journal, and usage."""
    store.create_run("Process the invoices in the AP mailbox", "invoice-processing", run_id=run_id)
    store.transition(run_id, RunState.RESOLVING)
    store.save_work_order(run_id, WorkOrder.model_validate(WORK_ORDER))
    store.save_plan(run_id, Plan.model_validate(PLAN))
    store.transition(run_id, RunState.PLANNED)
    store.transition(run_id, RunState.EXECUTING)
    store.add_observation(
        run_id,
        Observation(
            ok=True,
            summary="Archived the source invoice",
            data={"path": "processed/NW-2026-001.pdf"},
        ),
        step_position=2,
        tool="files.archive",
    )
    store.add_observation(
        run_id,
        Observation(
            ok=True, summary="Screenshot saved", artifacts=[f"runs/{run_id}/shot.png"]
        ),
        step_position=2,
        tool="browser.screenshot",
    )
    store.record_action(run_id, "file-1", "erp.file_invoice", {"number": "NW-2026-001"})
    store.complete_action(run_id, "file-1", {"invoice_id": "INV-3003"})
    store.increment_usage(run_id, steps=3, cost_usd=0.42)
    store.transition(run_id, RunState.VERIFYING)
    store.save_verification(
        run_id,
        [
            CheckResult(
                id="invoice-filed",
                description="The invoice exists in LedgerLite.",
                ok=True,
                detail="INV-3003 agrees with its source",
                evidence=["erp:INV-3003", "source:documents/invoices/NW-2026-001.pdf"],
            )
        ],
    )
    store.transition(run_id, RunState.COMPLETED)


def work_order_json(**overrides) -> str:
    return json.dumps({**WORK_ORDER, **overrides})


def plan_json(**overrides) -> str:
    return json.dumps({**PLAN, **overrides})
