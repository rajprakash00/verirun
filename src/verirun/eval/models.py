"""The eval report: per-Run quality metrics, and the aggregate over a run."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from verirun.engine.models import CheckResult
from verirun.engine.states import RunState


class ApprovalOutcome(BaseModel):
    """One human approval decision the eval recorded for a Run."""

    id: int
    tool: str
    action: str
    status: str
    executed: bool


class EscalationOutcome(BaseModel):
    """One open human question the eval recorded for a Run."""

    id: int
    reason: str
    question: str


class ScenarioReport(BaseModel):
    """Everything the eval measured for one seeded scenario."""

    key: str
    title: str
    run_id: str
    expected_state: RunState
    state: RunState | None = None
    parked_state: RunState | None = None
    verification: list[CheckResult] = Field(default_factory=list)
    verification_passed: bool = False
    approvals: list[ApprovalOutcome] = Field(default_factory=list)
    escalations: list[EscalationOutcome] = Field(default_factory=list)
    human_interventions: int = 0
    steps: int = 0
    cost_usd: float = 0.0
    resumed: bool = False
    resume_correct: bool | None = None
    regrounded_actions: int = 0
    skipped: bool = False
    passed: bool = False
    problems: list[str] = Field(default_factory=list)


class EvalTotals(BaseModel):
    """The aggregate across every scenario in one eval invocation."""

    scenarios: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    completed: int = 0
    steps: int = 0
    cost_usd: float = 0.0


class EvalReport(BaseModel):
    """The whole report one eval invocation writes."""

    generated_at: datetime
    mode: str = "replay"
    fixture_dir: str
    browser: bool
    scenarios: list[ScenarioReport] = Field(default_factory=list)
    totals: EvalTotals = Field(default_factory=EvalTotals)
    passed: bool = False
