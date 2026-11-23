"""How the API talks to the orchestrator. The protocol keeps the API testable without Temporal."""

from __future__ import annotations

import contextlib
from typing import Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError

from aieos.core.settings import TemporalSettings
from aieos.observability.telemetry import temporal_interceptors
from aieos.orchestration.models import ApprovalDecisionSignal, ClarificationAnswers, StartDeliveryInput
from aieos.orchestration.workflows import AGENT_QUEUE, FeatureDeliveryWorkflow


class Orchestrator(Protocol):
    async def start_delivery(self, temporal_workflow_id: str, start: StartDeliveryInput) -> None: ...
    async def signal_approval(self, temporal_workflow_id: str, signal: ApprovalDecisionSignal) -> None: ...
    async def signal_clarification(self, temporal_workflow_id: str, answers: ClarificationAnswers) -> None: ...
    async def request_cancel(self, temporal_workflow_id: str, reason: str) -> None: ...


async def connect(settings: TemporalSettings) -> Client:
    api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=api_key,
        tls=settings.temporal_tls or api_key is not None,
        data_converter=pydantic_data_converter,
        interceptors=temporal_interceptors(),
    )


class TemporalOrchestrator:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def start_delivery(self, temporal_workflow_id: str, start: StartDeliveryInput) -> None:
        # Idempotent: a retried create must not start a second run.
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await self._client.start_workflow(
                FeatureDeliveryWorkflow.run,
                start,
                id=temporal_workflow_id,
                task_queue=AGENT_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )

    async def signal_approval(self, temporal_workflow_id: str, signal: ApprovalDecisionSignal) -> None:
        await self._client.get_workflow_handle(temporal_workflow_id).signal(
            FeatureDeliveryWorkflow.approval_decided, signal
        )

    async def signal_clarification(self, temporal_workflow_id: str, answers: ClarificationAnswers) -> None:
        await self._client.get_workflow_handle(temporal_workflow_id).signal(
            FeatureDeliveryWorkflow.clarification_answered, answers
        )

    async def request_cancel(self, temporal_workflow_id: str, reason: str) -> None:
        await self._client.get_workflow_handle(temporal_workflow_id).signal(
            FeatureDeliveryWorkflow.cancel_requested, reason
        )
