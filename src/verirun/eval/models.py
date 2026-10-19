"""The eval report: per-Run quality metrics, and the aggregate over a run."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    actions: list[str] = Field(default_factory=list)
    resumed: bool = False
    resume_correct: bool | None = None
    regrounded_actions: int = 0
    skipped: bool = False
    passed: bool = False
    problems: list[str] = Field(default_factory=list)


class AblationCase(BaseModel):
    """One scenario compared across the baseline and the ablations."""

    key: str
    title: str
    kind: Literal["verifier", "approval_gates"]
    verifier_on_state: RunState | None = None
    verifier_off_state: RunState | None = None
    caught_checks: list[str] = Field(default_factory=list)
    undetected: bool = False
    gated_actions: list[str] = Field(default_factory=list)
    gates_off_state: RunState | None = None
    ungated_actions: list[str] = Field(default_factory=list)
    gates_off_escalations: int = 0


class AblationReport(BaseModel):
    """What changes when the Verifier or the Approval Gates are disabled."""

    probe_failures: int = 0
    caught_failures: int = 0
    caught_checks: int = 0
    detection_rate: float = 0.0
    undetected_failures: int = 0
    undetected_rate: float = 0.0
    gated_irreversible_actions: int = 0
    ungated_irreversible_actions: int = 0
    policy_denials: int = 0
    cases: list[AblationCase] = Field(default_factory=list)
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
    verifier: bool = True
    approval_gates: bool = True
    scenarios: list[ScenarioReport] = Field(default_factory=list)
    totals: EvalTotals = Field(default_factory=EvalTotals)
    ablations: AblationReport | None = None
    passed: bool = False
