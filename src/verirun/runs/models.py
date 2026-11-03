from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from verirun.context.models import WorkOrder
from verirun.engine.models import ErrorKind, Plan, Step
from verirun.engine.states import RunState


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    request: str
    task_id: str
    state: RunState
    work_order: WorkOrder | None = None
    plan: Plan | None = None
    error: str | None = None
    steps_used: int = 0
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime

    @property
    def steps(self) -> list[Step]:
        return self.plan.steps if self.plan else []


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    request: str
    state: RunState
    question: str | None = None
    created_at: datetime
    updated_at: datetime


JournalStatus = Literal["intent", "done", "failed", "unknown"]


class JournalEntry(BaseModel):
    """One side effect in the Run's outbox.

    ``intent`` is committed before the external call, ``done`` after it, and
    ``unknown`` when the call may have landed but its outcome could not be
    determined. ``unknown`` is never retried automatically.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    key: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JournalStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class SideEffectClaim(BaseModel):
    """One natural-key claim, such as a vendor invoice number, held by a Run."""

    model_config = ConfigDict(extra="forbid")

    claim_key: str
    run_id: str
    journal_key: str
    status: Literal["claimed", "done"]
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class EvidencePackRecord(BaseModel):
    """The index entry for one Run's Evidence Pack file."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    path: str
    sha256: str
    created_at: datetime


class RunException(BaseModel):
    """A Run parked because a side effect needs human reconciliation."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    journal_key: str | None = None
    kind: str
    summary: str
    question: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "resolved"] = "open"
    resolution: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class QueueItem(BaseModel):
    """One unit of worker work: start a Run, or resume a parked one."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    kind: Literal["start", "resume"]
    status: Literal["queued", "leased", "done", "dead"]
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    label: str
    state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    step_position: int | None = None
    tool: str | None = None
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error_kind: ErrorKind | None = None
    artifacts: list[str] = Field(default_factory=list)
    attempt: int = 1
    created_at: datetime


class Escalation(BaseModel):
    """A question for a human that parks the Run in ``needs_human``."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    reason: str
    question: str
    context: dict[str, Any] = Field(default_factory=dict)
    resolved: bool = False
    created_at: datetime


ApprovalStatus = Literal["pending", "approved", "rejected"]


class ApprovalRequest(BaseModel):
    """One prepared irreversible action waiting for a human yes/no."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    key: str
    step_position: int
    step_id: str
    tool: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy: str | None = None
    rule: str | None = None
    reason: str
    status: ApprovalStatus = "pending"
    decision_reason: str | None = None
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    created_at: datetime
