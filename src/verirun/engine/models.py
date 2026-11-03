from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorKind = Literal[
    "transient",
    "tool",
    "policy",
    "not_found",
    "invalid",
    "unknown",
    "ambiguity",
]

# The engine injects the Run's stable action key into a side-effect tool call.
# Tools never expose it to the model; adapters that can store it use it to
# recognise their own earlier write.
IDEMPOTENCY_KEY_ARG = "_idempotency_key"


class Step(BaseModel):
    """One piece of work inside a plan."""

    model_config = ConfigDict(extra="forbid")

    id: str
    goal: str
    allowed_tools: list[str] = Field(min_length=1)
    done_criterion: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[Step] = Field(min_length=1)


class Observation(BaseModel):
    """The result of one Tool call."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error_kind: ErrorKind | None = None
    artifacts: list[str] = Field(default_factory=list)


class CheckResult(BaseModel):
    """The Verifier's pass/fail for one criterion, with its evidence references."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    ok: bool
    detail: str = ""
    evidence: list[str] = Field(default_factory=list)
