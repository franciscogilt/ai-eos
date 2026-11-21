"""Messages of the feature-delivery workflow. Plain Pydantic models: safe inside the
Temporal workflow sandbox and serialised with the Pydantic data converter."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aieos.contracts.steps import AgentStepRequest, AgentStepResult

__all__ = ["AgentStepRequest", "AgentStepResult"]  # re-exported for workflow code


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeliveryLimits(_Msg):
    max_clarification_rounds: int = Field(default=3, ge=0, le=10)
    max_rework_rounds: int = Field(default=2, ge=0, le=5)
    clarification_timeout_hours: float = Field(default=7 * 24, gt=0)
    approval_poll_minutes: float = Field(default=5, gt=0)


class StartDeliveryInput(_Msg):
    org_id: UUID
    workflow_id: UUID
    project_id: UUID
    repository_id: UUID
    request: str = Field(min_length=1, max_length=20_000)
    requested_by: UUID
    limits: DeliveryLimits = DeliveryLimits()


class PlannedTaskRef(_Msg):
    key: str
    title: str
    description: str = ""
    depends_on: list[str] = []


# --------------------------------------------------------------------- progress events (persisted)
class WorkflowStatusEvent(_Msg):
    kind: Literal["workflow_status"] = "workflow_status"
    org_id: UUID
    workflow_id: UUID
    status: Literal["running", "awaiting_input", "awaiting_approval", "succeeded", "failed", "cancelled"]
    reason: str | None = None


class StepEvent(_Msg):
    kind: Literal["step"] = "step"
    org_id: UUID
    workflow_id: UUID
    step_key: str
    sequence: int
    attempt: int = 1
    status: Literal["running", "awaiting_input", "awaiting_approval", "succeeded", "failed", "cancelled"]
    error: str | None = None


class PlanEvent(_Msg):
    kind: Literal["plan"] = "plan"
    org_id: UUID
    workflow_id: UUID
    tasks: list[PlannedTaskRef]


class TaskEvent(_Msg):
    kind: Literal["task"] = "task"
    org_id: UUID
    workflow_id: UUID
    task_key: str
    status: Literal["running", "in_review", "succeeded", "failed", "cancelled"]
    attempt: int = 0


class GateEvent(_Msg):
    kind: Literal["gate"] = "gate"
    org_id: UUID
    workflow_id: UUID
    gate_key: str
    result: Literal["passed", "failed"]
    task_key: str | None = None
    details: dict[str, Any] = {}


ProgressEvent = Annotated[
    WorkflowStatusEvent | StepEvent | PlanEvent | TaskEvent | GateEvent, Field(discriminator="kind")
]


class ProgressEnvelope(_Msg):
    event: ProgressEvent


# --------------------------------------------------------------------- signals and queries
class ClarificationAnswers(_Msg):
    answers: dict[str, str] = Field(min_length=1)


class ApprovalDecisionSignal(_Msg):
    approval_id: UUID
    decision: Literal["approved", "rejected"]


class DeliveryStatus(_Msg):
    phase: str
    current_step: str | None
    open_questions: list[dict[str, Any]] = []
    pending_approval_id: UUID | None = None
    cancel_requested: bool = False


class DeliveryOutcome(_Msg):
    status: Literal["succeeded", "failed", "cancelled"]
    reason: str
    pull_request: dict[str, Any] | None = None
