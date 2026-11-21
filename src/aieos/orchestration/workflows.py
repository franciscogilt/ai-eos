"""Temporal workflow shell around `DeliveryProcess`.

Only this module touches the workflow API. Activities are referenced by name, so the
workflow sandbox never imports database, S3 or credential code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import cast
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from aieos.contracts.tools import ToolRequest, ToolResult
    from aieos.orchestration.models import (
        AgentStepRequest,
        AgentStepResult,
        ApprovalDecisionSignal,
        ClarificationAnswers,
        DeliveryOutcome,
        DeliveryStatus,
        ProgressEnvelope,
        StartDeliveryInput,
    )
    from aieos.orchestration.process import DeliveryProcess, StepFailedError

AGENT_QUEUE = "aieos-agents"
GATEWAY_QUEUE = "aieos-tool-gateway"

_QUICK = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=10,
)
_AGENT = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
    non_retryable_error_types=["BudgetExceeded", "AgentNotAvailable", "ContractViolation"],
)
_TOOL = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
)


class TemporalRuntime:
    """`Runtime` backed by Temporal activities and durable timers."""

    async def record(self, envelope: ProgressEnvelope) -> None:
        await workflow.execute_activity(
            "record_progress",
            envelope,
            task_queue=AGENT_QUEUE,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_QUICK,
        )

    async def run_agent_step(self, request: AgentStepRequest) -> AgentStepResult:
        try:
            return cast(
                AgentStepResult,
                await workflow.execute_activity(
                    "run_agent_step",
                    request,
                    result_type=AgentStepResult,
                    task_queue=AGENT_QUEUE,
                    start_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=_AGENT,
                ),
            )
        except ActivityError as exc:
            raise StepFailedError(f"{request.agent_key} step failed: {exc.cause}") from None

    async def call_tool(self, request: ToolRequest) -> ToolResult:
        try:
            return cast(
                ToolResult,
                await workflow.execute_activity(
                    "execute_tool",
                    request,
                    result_type=ToolResult,
                    task_queue=GATEWAY_QUEUE,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=_TOOL,
                ),
            )
        except ActivityError as exc:
            raise StepFailedError(f"{request.tool_name} could not be executed: {exc.cause}") from None

    async def supervisor_run(self, org_id: UUID, workflow_id: UUID) -> UUID:
        return cast(
            UUID,
            await workflow.execute_activity(
                "ensure_supervisor_run",
                args=[org_id, workflow_id],
                result_type=UUID,
                task_queue=AGENT_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_QUICK,
            ),
        )

    async def approval_status(self, org_id: UUID, approval_id: UUID) -> str:
        return cast(
            str,
            await workflow.execute_activity(
                "get_approval_status",
                args=[org_id, approval_id],
                result_type=str,
                task_queue=AGENT_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_QUICK,
            ),
        )

    async def wait(self, condition: Callable[[], bool], timeout: timedelta) -> bool:
        try:
            await workflow.wait_condition(condition, timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def cleanup(self, org_id: UUID, workflow_id: UUID) -> None:
        await workflow.execute_activity(
            "cleanup_workflow",
            args=[org_id, workflow_id],
            task_queue=AGENT_QUEUE,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_QUICK,
        )


@workflow.defn(name="FeatureDeliveryWorkflow")
class FeatureDeliveryWorkflow:
    @workflow.init
    def __init__(self, start: StartDeliveryInput) -> None:
        self._process = DeliveryProcess(TemporalRuntime(), start)

    @workflow.run
    async def run(self, start: StartDeliveryInput) -> DeliveryOutcome:
        try:
            return await self._process.run()
        except asyncio.CancelledError:
            # Temporal-level cancellation (e.g. `temporal workflow cancel`): clean up, then record.
            self._process.request_cancel("workflow cancelled")
            return await asyncio.shield(self._process._finish_cancelled("workflow cancelled"))

    @workflow.signal
    def clarification_answered(self, answers: ClarificationAnswers) -> None:
        self._process.answer(answers)

    @workflow.signal
    def approval_decided(self, decision: ApprovalDecisionSignal) -> None:
        self._process.decide(decision)

    @workflow.signal
    def cancel_requested(self, reason: str) -> None:
        self._process.request_cancel(reason)

    @workflow.query
    def status(self) -> DeliveryStatus:
        return self._process.status()
