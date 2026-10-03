"""The local dashboard: run list, run detail, and the approval queue.

A thin FastAPI layer over the Run store. It shows the Work Order, the Plan, a
live timeline of steps and observations, escalations, and the Approval queue.
Decisions post back into the engine: approve resumes the parked Run, reject
aborts it with the reason. A pending request never auto-approves.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from company_operator.config import Settings
from company_operator.context.company import load_company_context
from company_operator.context.task_pack import load_task_pack
from company_operator.engine.approve import approve, reject
from company_operator.engine.orchestrator import report_run, resume_run
from company_operator.engine.states import StepState
from company_operator.llm.client import LLMClient, build_client
from company_operator.runs.store import (
    ApprovalNotFoundError,
    RunNotFoundError,
    RunStore,
)
from company_operator.runtime import build_registry

TEMPLATE_DIR = Path(__file__).parent / "templates"

ClientFactory = Callable[[str], LLMClient]


def create_app(settings: Settings, *, client_factory: ClientFactory | None = None) -> FastAPI:
    """The dashboard for one Settings: the same Run store the CLI writes."""
    app = FastAPI(title="Operator", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    store = RunStore(settings.run_db)
    make_client = client_factory or (lambda run_id: build_client(settings, session_id=run_id))

    def detail(run_id: str) -> dict[str, Any]:
        try:
            run = store.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        steps = store.get_steps(run_id)
        states = store.get_step_states(run_id)
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
            "approvals": store.list_approvals(run_id),
            "escalations": store.list_escalations(run_id),
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
        run = store.get_run(run_id)
        context = load_company_context(settings.company_dir)
        task_pack = load_task_pack(settings.tasks_dir / f"{run.task_id}.yaml")
        resume_run(
            run_id,
            task_pack,
            make_client(run_id),
            store,
            build_registry(settings, context, task_pack),
            erp_db_path=settings.erp_db,
            shared_root=settings.shared_dir,
            evidence_root=settings.run_db.parent,
            max_steps=settings.max_steps,
            max_cost_usd=settings.max_cost_usd,
            prices=settings.prices,
        )

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
        return templates.TemplateResponse(
            request, "approvals.html", {"approvals": store.list_pending_approvals()}
        )

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
