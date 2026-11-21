"""Temporal activity for the tool-gateway worker (task queue `aieos-tool-gateway`)."""

from __future__ import annotations

from temporalio import activity
from temporalio.exceptions import ApplicationError

from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.gateway.service import ToolGateway, ToolInProgressError


class GatewayActivities:
    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    @activity.defn(name="execute_tool")
    async def execute_tool(self, request: ToolRequest) -> ToolResult:
        try:
            return await self._gateway.execute(request)
        except ToolInProgressError as exc:
            # Retryable: the activity retry policy backs off and tries again.
            raise ApplicationError(str(exc), type="ToolInProgress") from None
