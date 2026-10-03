from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from company_operator.context.models import WorkOrder
from company_operator.engine.models import ErrorKind, Plan, Step
from company_operator.engine.states import RunState


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


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    key: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["intent", "done", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None
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
