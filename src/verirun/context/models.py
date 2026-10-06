from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from verirun.context.policies import Policy, PolicySet


class Sop(BaseModel):
    id: str
    title: str
    body: str


class Precedent(BaseModel):
    id: str
    title: str
    body: str


class SystemEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    kind: str
    description: str = ""
    base_url: str | None = None
    root: str | None = None
    credentials: dict[str, str] = Field(default_factory=dict)


class ApprovalGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    policy: str | None = None


class WorkOrder(BaseModel):
    """Verirun's written interpretation of a Request, made before acting."""

    model_config = ConfigDict(extra="forbid")

    sop: str
    goal: str
    assumptions: list[str] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    approval_gates: list[ApprovalGate] = Field(default_factory=list)
    success_criteria: list[str] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)


class CompanyContextError(ValueError):
    pass


class CompanyContext:
    def __init__(
        self,
        root: Path,
        sops: dict[str, Sop],
        policies: PolicySet,
        systems: dict[str, SystemEntry],
        precedents: list[Precedent],
    ) -> None:
        self.root = root
        self.sops = sops
        self.policies = policies
        self.systems = systems
        self.precedents = precedents

    def sop(self, sop_id: str) -> Sop:
        if sop_id not in self.sops:
            raise CompanyContextError(f"no SOP '{sop_id}' in {self.root / 'sops'}")
        return self.sops[sop_id]

    def policy(self, policy_id: str) -> Policy:
        if policy_id not in self.policies:
            raise CompanyContextError(f"no policy '{policy_id}' in {self.root / 'policies'}")
        return self.policies[policy_id]

    def system(self, system_id: str) -> SystemEntry:
        if system_id not in self.systems:
            raise CompanyContextError(f"no system '{system_id}' in {self.root / 'systems.yaml'}")
        return self.systems[system_id]
