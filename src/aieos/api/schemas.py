"""Request and response bodies. Inputs reject unknown fields."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aieos.gateway.policy import ProjectPolicy
from aieos.persistence.repositories import OrgRow, ProjectRow, RepositoryRow

SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
GITHUB_LOGIN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
GITHUB_REPO = r"^[A-Za-z0-9._-]{1,100}$"
GIT_BRANCH_CHARS = r"^[A-Za-z0-9._/-]{1,200}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class MeOut(BaseModel):
    id: UUID
    email: str
    subject: str


class MyOrgOut(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str


class OrgOut(BaseModel):
    id: UUID
    slug: str
    name: str
    status: str
    role: str

    @classmethod
    def from_row(cls, row: OrgRow, role: str) -> OrgOut:
        return cls(id=row.id, slug=row.slug, name=row.name, status=row.status, role=role)


class ProjectCreate(_In):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    policy: ProjectPolicy = ProjectPolicy()


class ProjectUpdate(_In):
    version: int = Field(ge=1, description="Version the client last read (optimistic concurrency)")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    policy: ProjectPolicy | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> ProjectUpdate:
        if self.name is None and self.policy is None:
            raise ValueError("nothing to update")
        return self


class ProjectOut(BaseModel):
    id: UUID
    slug: str
    name: str
    policy: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: ProjectRow) -> ProjectOut:
        return cls(
            id=row.id,
            slug=row.slug,
            name=row.name,
            policy=row.policy,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class RepositoryCreate(_In):
    owner_login: str = Field(pattern=GITHUB_LOGIN)
    name: str = Field(pattern=GITHUB_REPO)
    external_id: int = Field(gt=0, description="GitHub repository id")
    github_installation_id: int = Field(gt=0)
    default_branch: str = Field(default="main", pattern=GIT_BRANCH_CHARS)

    @field_validator("default_branch")
    @classmethod
    def _valid_branch(cls, value: str) -> str:
        # Subset of git check-ref-format rules that matter for path safety.
        if value.startswith(("/", ".", "-")) or value.endswith(("/", ".", ".lock")) or ".." in value or "//" in value:
            raise ValueError("invalid branch name")
        return value


class RepositoryOut(BaseModel):
    id: UUID
    project_id: UUID
    provider: str
    external_id: int
    owner_login: str
    name: str
    full_name: str
    default_branch: str
    github_installation_id: int
    status: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: RepositoryRow) -> RepositoryOut:
        return cls(
            id=row.id,
            project_id=row.project_id,
            provider=row.provider,
            external_id=row.external_id,
            owner_login=row.owner_login,
            name=row.name,
            full_name=f"{row.owner_login}/{row.name}",
            default_branch=row.default_branch,
            github_installation_id=row.github_installation_id,
            status=row.status,
            created_at=row.created_at,
        )


# ---------------------------------------------------------------------------- workflows
class WorkflowCreate(_In):
    repository_id: UUID
    title: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=20_000)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")
    budget_usd: Decimal = Field(default=Decimal("25"), gt=0, le=1000, decimal_places=4)
    budget_tokens: int = Field(default=3_000_000, gt=0, le=100_000_000)


class WorkflowOut(BaseModel):
    id: UUID
    project_id: UUID
    repository_id: UUID
    kind: str
    title: str
    status: str
    budget_usd: Decimal
    spent_usd: Decimal
    budget_tokens: int
    spent_tokens: int
    created_by: UUID
    created_at: datetime
    finished_at: datetime | None


class CancelRequest(_In):
    reason: str = Field(default="cancelled by user", min_length=1, max_length=500)


class ClarificationRequest(_In):
    answers: dict[str, str] = Field(min_length=1)


# ---------------------------------------------------------------------------- approvals
class ApprovalOut(BaseModel):
    id: UUID
    workflow_id: UUID
    kind: str
    permission_level: int
    tool_name: str | None
    summary: str
    status: str
    decided_by_user_id: UUID | None
    decided_at: datetime | None
    decision_comment: str | None
    expires_at: datetime
    version: int
    created_at: datetime
    signal_delivered: bool | None = None


class ApprovalDecision(_In):
    decision: Literal["approve", "reject"]
    version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=2000)


class StepOut(BaseModel):
    step_key: str
    attempt: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None


class TaskOut(BaseModel):
    key: str
    title: str
    status: str
    attempt: int
    assigned_agent_key: str


class GateOut(BaseModel):
    gate_key: str
    task_key: str | None
    result: str
    details: dict[str, Any]
    evaluated_at: datetime


class RunOut(BaseModel):
    id: UUID
    agent_key: str
    step_key: str | None
    step_attempt: int | None
    model_provider: str
    model_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class TimelineOut(BaseModel):
    workflow: WorkflowOut
    steps: list[StepOut]
    tasks: list[TaskOut]
    gates: list[GateOut]
    runs: list[RunOut]


class CostRow(BaseModel):
    key: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    calls: int


class CostsOut(BaseModel):
    days: int
    total_usd: Decimal
    by_day: list[CostRow]
    by_agent: list[CostRow]
    by_model: list[CostRow]
