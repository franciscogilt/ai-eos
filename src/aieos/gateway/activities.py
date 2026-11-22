"""Temporal activity for the tool-gateway worker (task queue `aieos-tool-gateway`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.gateway.service import ToolGateway, ToolInProgressError
from aieos.sandbox.docker import DockerSandboxManager


class GatewayActivities:
    def __init__(self, gateway: ToolGateway, sandboxes: DockerSandboxManager | None = None) -> None:
        self._gateway = gateway
        self._sandboxes = sandboxes

    @activity.defn(name="execute_tool")
    async def execute_tool(self, request: ToolRequest) -> ToolResult:
        try:
            return await self._gateway.execute(request)
        except ToolInProgressError as exc:
            # Retryable: the activity retry policy backs off and tries again.
            raise ApplicationError(str(exc), type="ToolInProgress") from None

    @activity.defn(name="cleanup_sandboxes")
    async def cleanup_sandboxes(self, org_id: UUID, workflow_id: UUID) -> int:
        """Remove every sandbox of the workflow. Runs here because only this worker reaches Docker."""
        if self._sandboxes is None:
            return 0
        return await self._sandboxes.destroy_workflow(workflow_id)

    def all(self) -> list[Callable[..., Any]]:
        return [self.execute_tool, self.cleanup_sandboxes]
