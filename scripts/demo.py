"""Run the full Operator demo end to end against the seeded Mock Suite.

Three scenarios on one unchanged engine:

1. happy path — invoice processing completes and verifies;
2. transient failure — an ERP write fails once and the engine retries it;
3. over-limit approval — the Run parks at the payment gate, a human approves,
   and the resumed Run completes and verifies.

The demo is deterministic and offline: a scripted LLM client stands in for the
live model, so it is repeatable and free. With headless Chromium installed the
happy run also captures a real LedgerLite screenshot for the Evidence Pack;
pass --no-browser to skip that (or when Chromium is not installed).

Usage: uv run python scripts/demo.py [--state-dir DIR] [--shared DIR] [--runs DIR]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx
import uvicorn

from company_operator.config import Settings
from company_operator.context.company import CompanyContext, load_company_context
from company_operator.context.task_pack import TaskPack, load_task_pack
from company_operator.engine.approve import approve
from company_operator.engine.orchestrator import resume_run, run_task
from company_operator.engine.states import RunState
from company_operator.llm.client import AssistantTurn, ToolCall, Usage
from company_operator.runs.models import Run
from company_operator.runs.store import RunStore, generate_run_id
from company_operator.runtime import build_registry
from company_operator.tools import BrowserSession, ToolRegistry
from mocks.ledgerlite.app import create_app as create_ledgerlite_app
from mocks.seed.suite import reset_and_seed_suite

ROOT = Path(__file__).resolve().parents[1]

REQUEST = "Process the invoices in the AP mailbox"
FILED_INVOICE_ID = "INV-3003"

WORK_ORDER = {
    "sop": "invoice-processing",
    "goal": "File the September invoices from the AP mailbox",
    "assumptions": ["Only the September batch is in scope"],
    "systems": ["maildesk", "ledgerlite", "files"],
    "policies": ["spend-limits", "action-rules"],
    "approval_gates": [
        {
            "id": "over-spend-limit",
            "description": "Payments above 10,000 USD wait for a human",
            "policy": "spend-limits",
        }
    ],
    "success_criteria": ["Invoices are filed and verified in LedgerLite"],
    "open_questions": [],
}


def happy_plan(with_browser: bool) -> dict:
    step_three_tools = ["erp.schedule_payment", "files.archive"]
    if with_browser:
        step_three_tools += ["browser.navigate", "browser.screenshot"]
    return {
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
                "done_criterion": "The invoice is filed against PO-2001 and GR-2501",
            },
            {
                "id": "step-3",
                "goal": "Schedule the payment, archive the source, and capture the ERP record",
                "allowed_tools": step_three_tools,
                "done_criterion": "The payment is scheduled, the source is archived, and the record is captured",
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


class DemoLLM:
    """A deterministic scripted LLM client: the demo never calls a live model."""

    def __init__(self, replies: list[str | AssistantTurn]) -> None:
        self.replies = list(replies)

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model_role: str = "loop",
        response_format: dict | None = None,
    ) -> AssistantTurn:
        if not self.replies:
            raise AssertionError("the demo script ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, AssistantTurn):
            return reply
        return AssistantTurn(model="demo", text=reply, usage=Usage())


def text_turn(text: str) -> AssistantTurn:
    return AssistantTurn(model="demo", text=text, usage=Usage())


def tool_turn(name: str, arguments: dict, call_id: str) -> AssistantTurn:
    return AssistantTurn(
        model="demo",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        usage=Usage(),
    )


def happy_script(with_browser: bool, ledgerlite_url: str | None) -> DemoLLM:
    replies: list[str | AssistantTurn] = [
        json.dumps(WORK_ORDER),
        json.dumps(happy_plan(with_browser)),
        tool_turn("mail.read", {"message_id": "MSG-7001"}, "c1"),
        text_turn("Located NW-2026-001 in the batch email."),
        tool_turn("erp.get_purchase_order", {"po_id": "PO-2001"}, "c2"),
        tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2501"}, "c3"),
        tool_turn(
            "erp.file_invoice",
            {
                "number": "NW-2026-001",
                "vendor_id": "V-1001",
                "amount": 1250.00,
                "po_id": "PO-2001",
                "gr_id": "GR-2501",
            },
            "c4",
        ),
        text_turn(f"Filed as {FILED_INVOICE_ID}."),
        tool_turn("erp.schedule_payment", {"invoice_id": FILED_INVOICE_ID}, "c5"),
        tool_turn(
            "files.archive",
            {"path": "documents/invoices/NW-2026-001.pdf", "directory": "processed"},
            "c6",
        ),
    ]
    if with_browser and ledgerlite_url:
        replies += [
            tool_turn("browser.navigate", {"url": f"{ledgerlite_url}/invoices"}, "c7"),
            tool_turn(
                "browser.screenshot", {"name": "ledgerlite-invoices.png"}, "c8"
            ),
        ]
    replies.append(text_turn("Payment scheduled, source archived, and the ERP record captured."))
    return DemoLLM(replies)


def transient_script() -> DemoLLM:
    return DemoLLM(
        [
            json.dumps(WORK_ORDER),
            json.dumps(TRANSIENT_PLAN),
            tool_turn("erp.get_purchase_order", {"po_id": "PO-2007"}, "c1"),
            tool_turn("erp.get_goods_receipt", {"gr_id": "GR-2507"}, "c2"),
            text_turn("PO and goods receipt agree."),
            tool_turn(
                "erp.file_invoice",
                {
                    "number": "RS-2026-088",
                    "vendor_id": "V-1007",
                    "amount": 890.00,
                    "po_id": "PO-2007",
                    "gr_id": "GR-2507",
                },
                "c3",
            ),
            tool_turn("erp.schedule_payment", {"invoice_id": FILED_INVOICE_ID}, "c4"),
            text_turn("Filed and scheduled."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/RS-2026-088.pdf", "directory": "processed"},
                "c5",
            ),
            text_turn("Archived."),
        ]
    )


def over_limit_script() -> DemoLLM:
    return DemoLLM(
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
                "c1",
            ),
            text_turn("Filed SI-2026-550."),
            tool_turn("erp.schedule_payment", {"invoice_id": FILED_INVOICE_ID}, "c2"),
            # Consumed after the human approval resumes the Run.
            text_turn("Payment scheduled."),
            tool_turn(
                "files.archive",
                {"path": "documents/invoices/SI-2026-550.pdf", "directory": "processed"},
                "c3",
            ),
            text_turn("Archived."),
        ]
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "mocks" / "state")
    parser.add_argument("--shared", type=Path, default=ROOT / "shared")
    parser.add_argument("--runs", type=Path, default=ROOT / "runs")
    parser.add_argument(
        "--browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="capture a LedgerLite screenshot in the happy run (needs Chromium)",
    )
    return parser.parse_args(argv)


def start_browser(enabled: bool, artifact_dir: Path) -> BrowserSession | None:
    if not enabled:
        return None
    session = BrowserSession(artifact_dir=artifact_dir)
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - a missing Chromium only drops the screenshot
        session.close()
        print(f"warning: headless Chromium is unavailable ({exc})")
        print("         continuing without screenshots; install it with:")
        print("         uv run playwright install chromium")
        return None
    return session


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def serve_ledgerlite(db_path: Path) -> Iterator[str]:
    """Serve the seeded LedgerLite UI so the browser can capture it."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_ledgerlite_app(db_path), host="127.0.0.1", port=port, log_level="warning"
        )
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
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError(f"LedgerLite did not start on {base_url}")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run_happy(
    settings: Settings,
    context: CompanyContext,
    task_pack: TaskPack,
    store: RunStore,
    browser: BrowserSession | None,
    ledgerlite_url: str | None,
    run_id: str,
) -> Run:
    script = happy_script(browser is not None, ledgerlite_url)
    registry: ToolRegistry = build_registry(
        settings, context, task_pack, script, browser=browser
    )
    return run_task(
        REQUEST,
        task_pack,
        context,
        script,
        store,
        registry,
        erp_db_path=settings.erp_db,
        shared_root=settings.shared_dir,
        evidence_root=settings.run_db.parent,
        run_id=run_id,
    )


def run_transient_failure(
    settings: Settings, context: CompanyContext, task_pack: TaskPack, store: RunStore
) -> Run:
    script = transient_script()
    return run_task(
        REQUEST,
        task_pack,
        context,
        script,
        store,
        build_registry(settings, context, task_pack, script),
        erp_db_path=settings.erp_db,
        shared_root=settings.shared_dir,
        evidence_root=settings.run_db.parent,
        run_id=generate_run_id(),
    )


def run_approval(
    settings: Settings, context: CompanyContext, task_pack: TaskPack, store: RunStore
) -> tuple[Run, Run]:
    script = over_limit_script()
    registry = build_registry(settings, context, task_pack, script)
    run = run_task(
        REQUEST,
        task_pack,
        context,
        script,
        store,
        registry,
        erp_db_path=settings.erp_db,
        shared_root=settings.shared_dir,
        evidence_root=settings.run_db.parent,
        run_id=generate_run_id(),
    )
    request = store.open_approval(run.id)
    if request is None:
        raise RuntimeError("the over-limit Run did not park at its payment gate")
    print(f"  parked at the payment gate for {request.arguments.get('invoice_id')}")
    print(f"  human decision: approve (policy {request.policy}/{request.rule})")
    approve(store, request.id)
    resumed = resume_run(
        run.id,
        task_pack,
        script,
        store,
        build_registry(settings, context, task_pack, script),
        erp_db_path=settings.erp_db,
        shared_root=settings.shared_dir,
        evidence_root=settings.run_db.parent,
    )
    return run, resumed


def describe(store: RunStore, label: str, run: Run) -> None:
    html_path = store.path.parent / run.id / "evidence.html"
    verified = "verified" if run.state is RunState.COMPLETED else run.state.value
    print(
        f"  [{label}] {run.id}: {run.state.value} ({verified}) · "
        f"steps {run.steps_used} · ${run.cost_usd:.2f}"
    )
    print(f"           evidence: {html_path}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ledgerlite_db = args.state_dir / "ledgerlite.db"
    maildesk_db = args.state_dir / "maildesk.db"
    reset_and_seed_suite(ledgerlite_db, maildesk_db, args.shared)
    settings = Settings(
        _env_file=None,
        company_dir=ROOT / "company",
        tasks_dir=ROOT / "tasks",
        run_db=args.runs / "operator.db",
        shared_dir=args.shared,
        mail_db=maildesk_db,
        erp_db=ledgerlite_db,
    )
    store = RunStore(settings.run_db)
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")

    print("Operator demo — three scenarios on one unchanged engine")
    print(f"  company {settings.company_dir} · task pack {task_pack.id}")
    print()

    problems: list[str] = []
    happy_id = generate_run_id()
    with ExitStack() as stack:
        browser = start_browser(args.browser, settings.run_db.parent / happy_id)
        if browser is not None:
            stack.callback(browser.close)
            ledgerlite_url = stack.enter_context(serve_ledgerlite(ledgerlite_db))
        else:
            ledgerlite_url = None

        print("1/3 happy path: read, validate, file, schedule, archive")
        reset_and_seed_suite(ledgerlite_db, maildesk_db, args.shared)
        happy = run_happy(
            settings, context, task_pack, store, browser, ledgerlite_url, happy_id
        )
        describe(store, "happy", happy)
        if happy.state is not RunState.COMPLETED:
            problems.append(f"happy path ended in {happy.state.value}, expected completed")
        print()

        print("2/3 transient failure: the ERP write fails once, the engine retries")
        reset_and_seed_suite(ledgerlite_db, maildesk_db, args.shared)
        failure = run_transient_failure(settings, context, task_pack, store)
        describe(store, "transient", failure)
        if failure.state is not RunState.COMPLETED:
            problems.append(f"transient failure ended in {failure.state.value}, expected completed")
        print()

        print("3/3 over-limit approval: park at the gate, a human approves, resume")
        reset_and_seed_suite(ledgerlite_db, maildesk_db, args.shared)
        parked, approval = run_approval(settings, context, task_pack, store)
        describe(store, "approval", approval)
        if parked.state is not RunState.AWAITING_APPROVAL:
            problems.append(
                f"over-limit Run ended in {parked.state.value}, expected awaiting_approval"
            )
        if approval.state is not RunState.COMPLETED:
            problems.append(f"approved Run ended in {approval.state.value}, expected completed")
        print()

    if problems:
        for problem in problems:
            print(f"demo failed: {problem}", file=sys.stderr)
        return 1
    print("Demo complete. Open the evidence.html paths above for the full report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
