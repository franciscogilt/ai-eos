"""How an agent running inside an activity reaches the tool gateway.

Activities cannot schedule activities, and the agent worker must not hold the gateway's
credentials. Each tool call therefore runs as a tiny workflow whose only step is the
`execute_tool` activity on the gateway's task queue. The workflow id is derived from the
call's idempotency key, so a retried call attaches to the same execution.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from aieos.contracts.tools import ToolRequest, ToolResult

AGENT_QUEUE = "aieos-agents"
GATEWAY_QUEUE = "aieos-tool-gateway"


@workflow.defn(name="ToolCallWorkflow")
class ToolCallWorkflow:
    @workflow.run
    async def run(self, request: ToolRequest) -> ToolResult:
        return cast(
            ToolResult,
            await workflow.execute_activity(
                "execute_tool",
                request,
                result_type=ToolResult,
                task_queue=GATEWAY_QUEUE,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5),
            ),
        )


def tool_workflow_id(request: ToolRequest) -> str:
    return "tool-" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:40]


class TemporalToolInvoker:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def invoke(self, request: ToolRequest) -> ToolResult:
        wid = tool_workflow_id(request)
        try:
            handle = await self._client.start_workflow(
                ToolCallWorkflow.run,
                request,
                id=wid,
                task_queue=AGENT_QUEUE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                # A finished call is never scheduled again: its stored result is returned instead.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:  # finished earlier: read its result
            handle = self._client.get_workflow_handle_for(ToolCallWorkflow.run, wid)
        return await handle.result()
