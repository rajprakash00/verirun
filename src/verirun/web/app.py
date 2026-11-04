"""The local dashboard: run list, run detail, and the approval queue.

A thin FastAPI layer over the Run store. It shows the Work Order, the Plan, a
live timeline of steps and observations, escalations, and the Approval queue.
Decisions post back into the engine: approve resumes the parked Run, reject
aborts it with the reason. A pending request never auto-approves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from verirun.config import Settings
from verirun.engine.approve import approve, reject
from verirun.engine.orchestrator import report_run
from verirun.engine.states import StepState
from verirun.runs.store import (
    ApprovalNotFoundError,
    ExceptionNotFoundError,
    RunNotFoundError,
    RunStore,
)
from verirun.worker import enqueue_resume

TEMPLATE_DIR = Path(__file__).parent / "templates"

def create_app(settings: Settings) -> FastAPI:
    """The dashboard for one Settings: the same Run store the CLI writes.

    Mutating requests only record a decision and enqueue the Run. A separate
    worker process executes it, so no HTTP request waits on a model or an ERP.
    """
    app = FastAPI(title="Verirun", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    store = RunStore.for_settings(settings)

    def approval_review(approval: Any) -> dict[str, Any]:
        """The facts an approver needs: source, extraction, validations, bill.

        They come from the Run's own observations and the prepared arguments,
        so the queue shows the same evidence the Run will keep.
        """
        observations = store.list_observations(approval.run_id)
        number = approval.arguments.get("number")
        extractions = [
            item for item in observations if item.tool == "invoice.extract"
        ]
        chosen = None
        if isinstance(number, str) and number:
            for item in reversed(extractions):
                parsed = item.data.get("parsed") or {}
                if parsed.get("invoice_number") == number:
                    chosen = item
                    break
        if chosen is None and extractions:
            chosen = extractions[-1]
        extraction = chosen.data if chosen is not None else None
        source_path = (extraction or {}).get("path")
        source = None
        for item in reversed(observations):
            if item.tool != "mail.read" or not item.ok:
                continue
            attachments = item.data.get("attachments") or []
            if source_path is None or any(
                attachment.get("path") == source_path for attachment in attachments
            ):
                source = item.data
                break
        return {
            "request": approval,
            "source": source,
            "extraction": extraction,
            "validations": list((extraction or {}).get("validations") or []),
        }

    def detail(run_id: str) -> dict[str, Any]:
        try:
            run = store.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        steps = store.get_steps(run_id)
        states = store.get_step_states(run_id)
        approvals = store.list_approvals(run_id)
        return {
            "run": run,
            "steps": [
                {
                    "position": index,
                    "step": step,
                    "state": states[index] if index < len(states) else StepState.PENDING,
                }
                for index, step in enumerate(steps)
            ],
            "observations": store.list_observations(run_id),
            "approvals": approvals,
            "pending_reviews": [
                approval_review(request)
                for request in approvals
                if request.status == "pending"
            ],
            "escalations": store.list_escalations(run_id),
            "exceptions": store.list_exceptions(run_id),
            "verification": store.get_verification(run_id),
            "evidence_path": settings.run_db.parent / run_id / "evidence.json",
        }

    def pending_approval(approval_id: int) -> Any:
        try:
            request = store.get_approval(approval_id)
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if request.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Approval Request {approval_id} is already {request.status}",
            )
        return request

    def resume(run_id: str) -> None:
        """Queue a Run for the worker; the HTTP request never waits on it."""
        enqueue_resume(store, run_id)

    @app.get("/", response_class=HTMLResponse)
    def runs_page(request: Request) -> Any:
        return templates.TemplateResponse(
            request, "runs.html", {"runs": store.list_runs()}
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> Any:
        return templates.TemplateResponse(request, "run_detail.html", detail(run_id))

    @app.get("/runs/{run_id}/timeline", response_class=HTMLResponse)
    def timeline_fragment(request: Request, run_id: str) -> Any:
        return templates.TemplateResponse(request, "timeline.html", detail(run_id))

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request) -> Any:
        pending = store.list_pending_approvals()
        return templates.TemplateResponse(
            request,
            "approvals.html",
            {"approvals": [approval_review(item) for item in pending]},
        )

    @app.get("/exceptions", response_class=HTMLResponse)
    def exceptions_page(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "exceptions.html",
            {"exceptions": store.list_open_exceptions()},
        )

    @app.post("/exceptions/{exception_id}/resolve")
    def resolve_exception(
        exception_id: int,
        outcome: str = Form("acknowledged"),
        resolution: str = Form(""),
        bill_id: str = Form(""),
    ) -> RedirectResponse:
        try:
            exception = store.get_exception(exception_id)
        except ExceptionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = None
        if outcome == "landed":
            result = {
                "ok": True,
                "summary": resolution.strip()
                or f"Reconciled by a human as Exception {exception_id}",
                "data": {
                    "invoice_id": bill_id.strip(),
                    "reconciled": True,
                },
            }
        store.resolve_exception(
            exception_id,
            resolution=resolution.strip() or "resolved without a note",
            outcome=outcome,
            result=result,
        )
        enqueue_resume(store, exception.run_id)
        return RedirectResponse(f"/runs/{exception.run_id}", status_code=303)

    @app.post("/approvals/{approval_id}/approve")
    def approve_request(approval_id: int) -> RedirectResponse:
        request = pending_approval(approval_id)
        approve(store, approval_id)
        resume(request.run_id)
        return RedirectResponse(f"/runs/{request.run_id}", status_code=303)

    @app.post("/approvals/{approval_id}/reject")
    def reject_request(approval_id: int, reason: str = Form("")) -> RedirectResponse:
        request = pending_approval(approval_id)
        reject(store, approval_id, reason.strip() or "rejected without a reason")
        report_run(request.run_id, store, settings.run_db.parent)
        return RedirectResponse(f"/runs/{request.run_id}", status_code=303)

    return app
