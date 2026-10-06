from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from verirun.validation import format_validation_errors

ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

SCAN_TOOL = "files.extract"


class TaskPackError(ValueError):
    pass


class ApprovalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    policy: str | None = None
    actions: list[str] = Field(default_factory=list)


class ExtractionConfig(BaseModel):
    """How far a document extraction is trusted before a human is asked."""

    model_config = ConfigDict(extra="forbid")

    confidence_threshold: float = Field(ge=0.0, le=1.0)


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    check: str
    params: dict[str, Any] = Field(default_factory=dict)


def _reject_duplicates(values: list[str], label: str) -> list[str]:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")
    return values


class TaskPack(BaseModel):
    """The declarative definition of one kind of work."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    title: str = ""
    goal_template: str = Field(min_length=1)
    sop: str = Field(min_length=1)
    policies: list[str] = Field(default_factory=list)
    tools: list[str] = Field(min_length=1)
    approval_rules: list[ApprovalRule] = Field(default_factory=list)
    verification: list[VerificationCheck] = Field(default_factory=list)
    extraction: ExtractionConfig | None = None

    @field_validator("policies")
    @classmethod
    def _unique_policies(cls, policies: list[str]) -> list[str]:
        return _reject_duplicates(policies, "policy")

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, tools: list[str]) -> list[str]:
        return _reject_duplicates(tools, "tool names")

    @field_validator("approval_rules")
    @classmethod
    def _unique_approval_rules(cls, rules: list[ApprovalRule]) -> list[ApprovalRule]:
        _reject_duplicates([rule.id for rule in rules], "approval rule")
        return rules

    @field_validator("verification")
    @classmethod
    def _unique_verification(cls, checks: list[VerificationCheck]) -> list[VerificationCheck]:
        _reject_duplicates([check.id for check in checks], "verification check")
        return checks

    @model_validator(mode="after")
    def _scan_tool_needs_a_threshold(self) -> TaskPack:
        if SCAN_TOOL in self.tools and self.extraction is None:
            raise ValueError(
                f"tool '{SCAN_TOOL}' is allowlisted but no extraction.confidence_threshold is set"
            )
        return self

    def policy_ids(self) -> list[str]:
        return list(dict.fromkeys(self.policies))


def load_task_pack(path: str | Path) -> TaskPack:
    path = Path(path)
    if not path.is_file():
        raise TaskPackError(f"Task Pack not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaskPackError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskPackError(f"invalid Task Pack {path}: expected a YAML mapping at the top level")
    try:
        return TaskPack.model_validate(data)
    except ValidationError as exc:
        raise TaskPackError(
            f"invalid Task Pack {path}: {format_validation_errors(exc)}"
        ) from exc
