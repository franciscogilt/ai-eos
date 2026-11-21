"""One agent step: what the orchestrator asks for and what it gets back.

Shared by the orchestrator (which schedules steps) and the agent runner (which executes them).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentStepRequest(_Msg):
    org_id: UUID
    workflow_id: UUID
    agent_key: str
    step_key: str
    attempt: int = 1
    inputs: dict[str, Any] = {}


class AgentStepResult(_Msg):
    status: Literal["completed", "blocked"]
    summary: str
    output: dict[str, Any] = {}
    artifact_ids: list[UUID] = []
    agent_run_id: UUID | None = None
