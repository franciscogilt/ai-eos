"""Tool-call messages exchanged between the orchestrator and the tool gateway.

They cross a Temporal task-queue boundary, so they are plain, serialisable models.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    org_id: UUID
    workflow_id: UUID
    agent_run_id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = {}
    # Stable per logical call within the workflow; a retry or re-request reuses it.
    call_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")

    @property
    def idempotency_key(self) -> str:
        return f"{self.workflow_id}/{self.agent_run_id}/{self.call_id}"


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed", "denied", "awaiting_approval"]
    reason: str
    tool_execution_id: UUID | None = None
    result: dict[str, Any] | None = None
    approval_id: UUID | None = None
