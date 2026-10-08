"""Real-browser tests for browser re-grounding.

A browser Step that loses its target ref recovers: the tool takes a fresh
snapshot, re-resolves the control by role and accessible name, and retries with
the new ref. When the accessibility tree does not contain the control, the
vision fallback inspects a screenshot. The successful locator is recorded in
the observation, so the Run journal replays it on a retry and never repeats a
completed side effect. Recovery failures stay inside the existing failure
ladder: alternate strategy, re-plan, escalate.

The seeded "UI changed" scenario lives in LedgerLite: the new-invoice page
re-renders itself right after the first snapshot (``?ui=changed``), and the
``?ui=painted`` variant turns the submit button into a non-semantic div.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mocks.ledgerlite import db
from tests.support import (
    WORK_ORDER,
    ScriptedClient,
    find_ref,
    location_turn,
    ref_for,
    run_settings,
    text_turn,
    tool_turn,
)
from verirun.config import DEFAULT_PRICES
from verirun.context.company import load_company_context
from verirun.context.models import WorkOrder
from verirun.context.task_pack import load_task_pack
from verirun.engine.execute import execute_run
from verirun.engine.models import Plan
from verirun.engine.states import RunState
from verirun.llm.client import AssistantTurn, Message, ToolSpec
from verirun.runs.store import RunStore
from verirun.runtime import build_registry
from verirun.tools import BrowserSession, ToolRegistry, build_browser_tools

BROWSER_TOOLS = [
    "browser.navigate",
    "browser.snapshot",
    "browser.click",
    "browser.type",
    "browser.select",
    "browser.extract",
    "browser.screenshot",
]


@pytest.fixture(scope="module")
def chromium(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Skip the browser tests when Chromium is not installed.

    The check starts and closes its own session before yielding: the sync
    Playwright API allows only one session per thread at a time.
    """
    session = BrowserSession(artifact_dir=tmp_path_factory.mktemp("chromium") / "artifacts")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - report missing system dependencies as a skip
        pytest.skip(
            f"Chromium is unavailable ({exc}); "
            "run `uv run playwright install --with-deps chromium`"
        )
    finally:
        session.close()
    yield


@pytest.fixture
def browser(chromium: None, tmp_path: Path) -> Iterator[BrowserSession]:
    session = BrowserSession(artifact_dir=tmp_path / "artifacts")
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001 - report missing system dependencies as a skip
        session.close()
        pytest.skip(f"Chromium is unavailable ({exc})")
    try:
        yield session
    finally:
        session.close()


def test_a_stale_ref_regrounds_by_role_and_name_after_the_ui_changes(
    browser: BrowserSession, ledgerlite_server: str, ledgerlite_db: Path
) -> None:
    registry = ToolRegistry(build_browser_tools(browser), allowlist=BROWSER_TOOLS)
    assert registry.invoke(
        "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new?ui=changed"}
    ).ok
    form = registry.invoke("browser.snapshot")
    number = ref_for(form, role="textbox", name="Vendor invoice number")
    vendor = ref_for(form, role="combobox", name="Vendor")
    purchase_order = ref_for(form, role="combobox", name="Purchase order")
    goods_receipt = ref_for(form, role="combobox", name="Goods receipt")
    amount = ref_for(form, role="textbox", name="Amount")
    submit = ref_for(form, role="button", name="File invoice")
    browser.page.wait_for_function("window.uiChanged === true")
    assert browser.page.locator("[data-op-ref]").count() == 0

    typed = registry.invoke("browser.type", {"ref": number, "text": "NW-2026-901"})

    assert typed.ok, typed.summary
    assert typed.data["ref"] == number
    assert typed.data["regrounded"] is True
    assert typed.data["locator"]["method"] == "snapshot"
    assert typed.data["locator"]["role"] == "textbox"
    assert typed.data["locator"]["name"] == "Vendor invoice number"
    assert typed.data["locator"]["ref"] != number

    replayed = registry.invoke("browser.type", {"ref": number, "text": "NW-2026-901"})

    assert replayed.ok, replayed.summary
    assert replayed.data["locator"]["name"] == "Vendor invoice number"

    assert registry.invoke("browser.select", {"ref": vendor, "value": "V-1001"}).ok
    assert registry.invoke("browser.select", {"ref": purchase_order, "value": "PO-2001"}).ok
    assert registry.invoke("browser.select", {"ref": goods_receipt, "value": "GR-2501"}).ok
    amount_typed = registry.invoke("browser.type", {"ref": amount, "text": "1250.00"})
    assert amount_typed.ok, amount_typed.summary
    assert amount_typed.data["regrounded"] is True

    filed = registry.invoke("browser.click", {"ref": submit})

    assert filed.ok, filed.summary
    assert filed.data["regrounded"] is True
    assert "/invoices/INV-3003" in filed.data["url"]
    with closing(db.connect(ledgerlite_db)) as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE number = 'NW-2026-901'"
        ).fetchone()
    assert row is not None
    assert row["vendor_id"] == "V-1001"
    assert row["po_id"] == "PO-2001"
    assert row["gr_id"] == "GR-2501"
    assert row["amount_cents"] == 125_000
    assert row["status"] == "received"


def test_a_control_missing_from_the_tree_uses_the_vision_fallback(
    chromium: None, tmp_path: Path, ledgerlite_server: str
) -> None:
    script = ScriptedClient([])
    session = BrowserSession(
        artifact_dir=tmp_path / "artifacts", client=script, prices=DEFAULT_PRICES
    )
    session.start()
    try:
        registry = ToolRegistry(build_browser_tools(session), allowlist=BROWSER_TOOLS)
        assert registry.invoke(
            "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new?ui=painted"}
        ).ok
        form = registry.invoke("browser.snapshot")
        submit = ref_for(form, role="button", name="File invoice")
        session.page.wait_for_function("window.uiChanged === true")

        # The changed layout has no semantic submit button; only vision can reach it.
        assert session.page.locator("button[type=submit]").count() == 0
        assert session.page.locator("#painted-submit").count() == 1

        point = session.page.evaluate(
            """() => {
                const rect = document.getElementById("painted-submit").getBoundingClientRect();
                return {
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                };
            }"""
        )
        script.replies.append(location_turn(point["x"], point["y"]))

        clicked = registry.invoke("browser.click", {"ref": submit})

        assert clicked.ok, clicked.summary
        assert clicked.data["regrounded"] is True
        assert clicked.data["locator"]["method"] == "vision"
        assert clicked.data["locator"]["point"] == [point["x"], point["y"]]
        assert clicked.data["locator"]["confidence"] == pytest.approx(0.9)
        assert clicked.data["usage"]["prompt_tokens"] == 1200
        assert clicked.data["cost_usd"] > 0
        assert session.page.evaluate("window.paintedClicks") == 1
        assert script.calls[-1]["model_role"] == "vision"
        assert clicked.artifacts
        assert all(Path(path).is_file() for path in clicked.artifacts)

        # A retry replays the vision locator without a second model call.
        model_calls = len(script.calls)
        again = registry.invoke("browser.click", {"ref": submit})

        assert again.ok, again.summary
        assert again.data["locator"]["method"] == "replay"
        assert again.data["locator"]["point"] == [point["x"], point["y"]]
        assert len(script.calls) == model_calls
        assert session.page.evaluate("window.paintedClicks") == 2
    finally:
        session.close()


def test_the_vision_fallback_refuses_a_point_that_hits_another_control(
    chromium: None, tmp_path: Path, ledgerlite_server: str
) -> None:
    script = ScriptedClient([])
    session = BrowserSession(artifact_dir=tmp_path / "artifacts", client=script)
    session.start()
    try:
        registry = ToolRegistry(build_browser_tools(session), allowlist=BROWSER_TOOLS)
        assert registry.invoke(
            "browser.navigate", {"url": f"{ledgerlite_server}/invoices/new?ui=painted"}
        ).ok
        form = registry.invoke("browser.snapshot")
        submit = ref_for(form, role="button", name="File invoice")
        session.page.wait_for_function("window.uiChanged === true")
        wrong_point = session.page.evaluate(
            """() => {
                const link = Array.from(document.querySelectorAll("a"))
                    .find((node) => node.textContent.trim() === "Invoices");
                const rect = link.getBoundingClientRect();
                return {
                    x: Math.round(rect.x + rect.width / 2),
                    y: Math.round(rect.y + rect.height / 2),
                };
            }"""
        )
        script.replies.append(location_turn(wrong_point["x"], wrong_point["y"]))

        clicked = registry.invoke("browser.click", {"ref": submit})

        assert not clicked.ok
        assert clicked.error_kind == "not_found"
        assert "not a button" in clicked.summary
        assert session.page.evaluate("window.paintedClicks || 0") == 0
    finally:
        session.close()


BROWSER_PLAN = {
    "steps": [
        {
            "id": "step-1",
            "goal": "File the Northwind invoice through the LedgerLite browser UI",
            "allowed_tools": [
                "browser.navigate",
                "browser.snapshot",
                "browser.type",
                "browser.select",
                "browser.click",
            ],
            "done_criterion": "NW-2026-901 is filed in LedgerLite",
        }
    ]
}


def _latest_refs(messages: list[Message]) -> dict[str, dict[str, Any]]:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        payload = json.loads(message["content"])
        refs = (payload.get("data") or {}).get("refs")
        if refs:
            return refs
    raise AssertionError("no snapshot observation in the engine messages")


class BrowserStepClient:
    """Drives one browser step, reading snapshot refs out of the engine messages."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: list[dict[str, Any]] = []
        self.submit_args: dict[str, Any] | None = None

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        model_role: str = "loop",
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        self.calls.append({"messages": messages, "tools": tools, "model_role": model_role})
        step = len(self.calls) - 1
        if step == 0:
            return tool_turn("browser.navigate", {"url": self.url}, call_id="c1")
        if step == 1:
            return tool_turn("browser.snapshot", call_id="c2")
        refs = _latest_refs(messages)
        if step == 2:
            return tool_turn(
                "browser.type",
                {"ref": find_ref(refs, role="textbox", name="Vendor invoice number"), "text": "NW-2026-901"},
                call_id="c3",
            )
        if step == 3:
            return tool_turn(
                "browser.select",
                {"ref": find_ref(refs, role="combobox", name="Vendor"), "value": "V-1001"},
                call_id="c4",
            )
        if step == 4:
            return tool_turn(
                "browser.select",
                {"ref": find_ref(refs, role="combobox", name="Purchase order"), "value": "PO-2001"},
                call_id="c5",
            )
        if step == 5:
            return tool_turn(
                "browser.select",
                {"ref": find_ref(refs, role="combobox", name="Goods receipt"), "value": "GR-2501"},
                call_id="c6",
            )
        if step == 6:
            return tool_turn(
                "browser.type",
                {"ref": find_ref(refs, role="textbox", name="Amount"), "text": "1250.00"},
                call_id="c7",
            )
        if step in (7, 8):
            if self.submit_args is None:
                self.submit_args = {"ref": find_ref(refs, role="button", name="File invoice")}
            return tool_turn("browser.click", dict(self.submit_args), call_id=f"c{step + 1}")
        return text_turn("Filed the invoice through the browser.")


def planned_run(store: RunStore, run_id: str = "RUN-BROWSER-1") -> str:
    store.create_run(
        "file the Northwind invoice through the browser UI",
        "invoice-processing",
        run_id=run_id,
    )
    store.transition(run_id, RunState.RESOLVING)
    store.save_work_order(run_id, WorkOrder.model_validate(WORK_ORDER))
    store.save_plan(run_id, Plan.model_validate(BROWSER_PLAN))
    store.transition(run_id, RunState.PLANNED)
    return run_id


def test_a_regrounded_locator_is_journaled_and_a_retry_replays_it(
    chromium: None,
    tmp_path: Path,
    ledgerlite_db: Path,
    maildesk_state,
    ledgerlite_server: str,
) -> None:
    settings = run_settings(
        tmp_path, ledgerlite_db=ledgerlite_db, maildesk_state=maildesk_state
    )
    context = load_company_context(settings.company_dir)
    task_pack = load_task_pack(settings.tasks_dir / "invoice-processing.yaml")
    store = RunStore(settings.run_db)
    client = BrowserStepClient(f"{ledgerlite_server}/invoices/new?ui=changed")
    session = BrowserSession(artifact_dir=tmp_path / "artifacts")
    session.start()
    try:
        registry = build_registry(settings, context, task_pack, client, browser=session)
        run_id = planned_run(store)
        run = execute_run(run_id, client, store, registry, task_pack=None)
    finally:
        session.close()

    # The click submitted the form and the page moved on, so a second, re-run
    # click could not succeed; both observations being ok proves the engine
    # replayed the journaled locator instead of touching the browser again.
    assert run.state is RunState.VERIFYING
    journal = [entry for entry in store.list_journal(run_id) if entry.action == "browser.click"]
    assert len(journal) == 1
    assert journal[0].status == "done"
    locator = (journal[0].result or {})["data"]["locator"]
    assert locator["method"] == "snapshot"
    assert locator["role"] == "button"
    assert locator["name"] == "File invoice"
    clicks = [record for record in store.list_observations(run_id) if record.tool == "browser.click"]
    assert [record.ok for record in clicks] == [True, True]
    with closing(db.connect(ledgerlite_db)) as conn:
        filed = conn.execute(
            "SELECT * FROM invoices WHERE number = 'NW-2026-901'"
        ).fetchall()
    assert len(filed) == 1


def test_a_failed_regrounding_stays_bounded_by_the_failure_ladder(
    chromium: None, tmp_path: Path
) -> None:
    store = RunStore(tmp_path / "runs" / "verirun.db")
    client = ScriptedClient([tool_turn("browser.click", {"ref": "e999"}, call_id="c1")])
    session = BrowserSession(artifact_dir=tmp_path / "artifacts")
    session.start()
    try:
        registry = ToolRegistry(build_browser_tools(session), allowlist=BROWSER_TOOLS)
        run_id = store.create_run("click a control that is gone", "invoice-processing").id
        store.transition(run_id, RunState.RESOLVING)
        store.save_work_order(run_id, WorkOrder.model_validate(WORK_ORDER))
        store.save_plan(
            run_id,
            Plan.model_validate(
                {
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "Click a control that no longer exists",
                            "allowed_tools": ["browser.click"],
                            "done_criterion": "The control was clicked",
                        }
                    ]
                }
            ),
        )
        store.transition(run_id, RunState.PLANNED)
        run = execute_run(
            run_id,
            client,
            store,
            registry,
            max_attempts_per_step=1,
            max_replans=0,
        )
    finally:
        session.close()

    assert run.state is RunState.NEEDS_HUMAN
    attempts = [
        record for record in store.list_observations(run_id) if record.tool == "browser.click"
    ]
    assert len(attempts) == 1
    assert attempts[0].error_kind == "not_found"
    escalation = store.open_escalation(run_id)
    assert escalation is not None
    assert "e999" in escalation.question
