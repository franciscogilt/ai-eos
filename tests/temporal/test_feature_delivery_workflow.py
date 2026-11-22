"""FeatureDeliveryWorkflow on a real Temporal test server (time-skipping), with fake activities.

Needs the Temporal test server binary, which the SDK downloads on first use. Where that
download is not possible the tests skip, unless AIEOS_REQUIRE_TEMPORAL=1 (set in CI), in
which case they fail.
"""

from __future__ import annotations

import asyncio
import functools
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.orchestration.models import (
    AgentStepRequest,
    AgentStepResult,
    ApprovalDecisionSignal,
    DeliveryLimits,
    ProgressEnvelope,
    StartDeliveryInput,
)
from aieos.orchestration.workflows import AGENT_QUEUE, GATEWAY_QUEUE, FeatureDeliveryWorkflow

pytestmark = pytest.mark.temporal

APPROVAL_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")


class Fakes:
    """Activity implementations with the production activity names."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.tool_calls: list[ToolRequest] = []
        self.approval_db = "pending"
        self.cleaned = False
        self.sandboxes_cleaned = False

    @activity.defn(name="record_progress")
    async def record_progress(self, envelope: ProgressEnvelope) -> None:
        self.events.append(envelope.event)

    @activity.defn(name="run_agent_step")
    async def run_agent_step(self, request: AgentStepRequest) -> AgentStepResult:
        output = {
            "requirements": {"open_questions": []},
            "architecture": {"tasks": [{"key": "T-1", "title": "Only task"}]},
            "developer": {"patch_artifact_id": "p"},
            "qa_security": {"verdict": "approve"},
        }[request.agent_key]
        return AgentStepResult(status="completed", summary="ok", output=output, agent_run_id=uuid.uuid4())

    @activity.defn(name="ensure_supervisor_run")
    async def ensure_supervisor_run(self, org_id: uuid.UUID, workflow_id: uuid.UUID) -> uuid.UUID:
        return uuid.UUID(int=1)

    @activity.defn(name="get_approval_status")
    async def get_approval_status(self, org_id: uuid.UUID, approval_id: uuid.UUID) -> str:
        return self.approval_db

    @activity.defn(name="cleanup_workflow")
    async def cleanup_workflow(self, org_id: uuid.UUID, workflow_id: uuid.UUID) -> None:
        self.cleaned = True

    @activity.defn(name="execute_tool")
    async def execute_tool(self, request: ToolRequest) -> ToolResult:
        self.tool_calls.append(request)
        merges = [c for c in self.tool_calls if c.tool_name == "github.merge_pull_request"]
        if request.tool_name == "github.merge_pull_request" and len(merges) == 1:
            return ToolResult(status="awaiting_approval", reason="human approval required", approval_id=APPROVAL_ID)
        return ToolResult(status="succeeded", reason="ok", result={"number": 42})

    @activity.defn(name="cleanup_sandboxes")
    async def cleanup_sandboxes(self, org_id: uuid.UUID, workflow_id: uuid.UUID) -> int:
        self.sandboxes_cleaned = True
        return 0

    def gateway_activities(self) -> list[Any]:
        return [self.execute_tool, self.cleanup_sandboxes]

    def agent_activities(self) -> list[Any]:
        return [
            self.record_progress,
            self.run_agent_step,
            self.ensure_supervisor_run,
            self.get_approval_status,
            self.cleanup_workflow,
        ]


TEST_TIMEOUT_SECONDS = 60


def bounded(test: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Fail a hanging workflow test with a traceback instead of letting it consume the CI job."""

    @functools.wraps(test)
    async def wrapper(*args: Any, **kwargs: Any) -> None:
        await asyncio.wait_for(test(*args, **kwargs), TEST_TIMEOUT_SECONDS)

    return wrapper


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    try:
        environment = await asyncio.wait_for(
            WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter), timeout=120
        )
    except RuntimeError as exc:
        if os.environ.get("AIEOS_REQUIRE_TEMPORAL") == "1":
            raise
        pytest.skip(f"Temporal test server unavailable here: {exc}")
    async with environment:
        yield environment


def start_input() -> StartDeliveryInput:
    return StartDeliveryInput(
        org_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        request="Add CSV export",
        requested_by=uuid.uuid4(),
        limits=DeliveryLimits(approval_poll_minutes=5),
    )


def workers(env: WorkflowEnvironment, fakes: Fakes) -> tuple[Worker, Worker]:
    agent = Worker(
        env.client, task_queue=AGENT_QUEUE, workflows=[FeatureDeliveryWorkflow], activities=fakes.agent_activities()
    )
    gateway = Worker(env.client, task_queue=GATEWAY_QUEUE, activities=fakes.gateway_activities())
    return agent, gateway


async def wait_for_phase(handle: Any, phase: str) -> None:
    for _ in range(200):
        status = await handle.query(FeatureDeliveryWorkflow.status)
        if status.phase == phase:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {phase}")


@pytest.mark.asyncio
@bounded
async def test_happy_path_with_approval_signal(env: WorkflowEnvironment) -> None:
    fakes = Fakes()
    agent, gateway = workers(env, fakes)
    async with agent, gateway:
        handle = await env.client.start_workflow(
            FeatureDeliveryWorkflow.run, start_input(), id=f"wf-{uuid.uuid4()}", task_queue=AGENT_QUEUE
        )
        await wait_for_phase(handle, "awaiting_approval")
        await handle.signal(
            FeatureDeliveryWorkflow.approval_decided,
            ApprovalDecisionSignal(approval_id=APPROVAL_ID, decision="approved"),
        )
        outcome = await handle.result()
    assert outcome.status == "succeeded" and outcome.pull_request == {"number": 42}
    assert [c.tool_name for c in fakes.tool_calls] == [
        "github.create_pull_request",
        "github.merge_pull_request",
        "github.merge_pull_request",
    ]


@pytest.mark.asyncio
@bounded
async def test_resumes_after_worker_restart(env: WorkflowEnvironment) -> None:
    """B10 acceptance: kill the worker mid-workflow; a new worker resumes from history.

    Both workers run with workflow caching disabled. That removes sticky task queues (which
    the time-skipping test server never hands over to another worker) and forces every
    workflow task to be replayed from history, so the test also proves the workflow is
    deterministic under replay at every step.
    """
    fakes = Fakes()
    gateway = Worker(env.client, task_queue=GATEWAY_QUEUE, activities=fakes.gateway_activities())

    def agent_worker() -> Worker:
        return Worker(
            env.client,
            task_queue=AGENT_QUEUE,
            workflows=[FeatureDeliveryWorkflow],
            activities=fakes.agent_activities(),
            max_cached_workflows=0,
        )

    async with gateway:
        async with agent_worker():
            handle = await env.client.start_workflow(
                FeatureDeliveryWorkflow.run, start_input(), id=f"wf-{uuid.uuid4()}", task_queue=AGENT_QUEUE
            )
            await wait_for_phase(handle, "awaiting_approval")
        # First worker is gone. Signal while no worker runs; a brand-new worker picks the workflow up.
        await handle.signal(
            FeatureDeliveryWorkflow.approval_decided,
            ApprovalDecisionSignal(approval_id=APPROVAL_ID, decision="approved"),
        )
        async with agent_worker():
            outcome = await handle.result()
    assert outcome.status == "succeeded"
    # Steps completed before the restart were not re-run.
    assert [e.step_key for e in fakes.events if e.kind == "step" and e.status == "running"].count("requirements") == 1


@pytest.mark.asyncio
@bounded
async def test_lost_signal_recovered_by_poll(env: WorkflowEnvironment) -> None:
    fakes = Fakes()
    agent, gateway = workers(env, fakes)
    async with agent, gateway:
        handle = await env.client.start_workflow(
            FeatureDeliveryWorkflow.run, start_input(), id=f"wf-{uuid.uuid4()}", task_queue=AGENT_QUEUE
        )
        await wait_for_phase(handle, "awaiting_approval")
        fakes.approval_db = "approved"  # decided in the database, signal never sent
        await env.sleep(timedelta(minutes=6))  # time-skipping past one poll interval
        outcome = await handle.result()
    assert outcome.status == "succeeded"


@pytest.mark.asyncio
@bounded
async def test_cancel_signal_cleans_up(env: WorkflowEnvironment) -> None:
    fakes = Fakes()
    agent, gateway = workers(env, fakes)
    async with agent, gateway:
        handle = await env.client.start_workflow(
            FeatureDeliveryWorkflow.run, start_input(), id=f"wf-{uuid.uuid4()}", task_queue=AGENT_QUEUE
        )
        await wait_for_phase(handle, "awaiting_approval")
        await handle.signal(FeatureDeliveryWorkflow.cancel_requested, "no longer needed")
        outcome = await handle.result()
    assert outcome.status == "cancelled" and fakes.cleaned and fakes.sandboxes_cleaned
    assert fakes.events[-1].kind == "workflow_status" and fakes.events[-1].status == "cancelled"


@pytest.mark.asyncio
@bounded
async def test_temporal_cancel_records_cancelled(env: WorkflowEnvironment) -> None:
    fakes = Fakes()
    agent, gateway = workers(env, fakes)
    async with agent, gateway:
        handle = await env.client.start_workflow(
            FeatureDeliveryWorkflow.run, start_input(), id=f"wf-{uuid.uuid4()}", task_queue=AGENT_QUEUE
        )
        await wait_for_phase(handle, "awaiting_approval")
        await handle.cancel()
        try:
            outcome = await handle.result()
            assert outcome.status == "cancelled"
        except WorkflowFailureError:
            pytest.fail("cancellation should end in a recorded 'cancelled' outcome, not a failure")
    assert fakes.cleaned


@pytest.mark.asyncio
@bounded
async def test_tool_bridge_runs_each_call_once(env: WorkflowEnvironment) -> None:
    from aieos.orchestration.tool_bridge import TemporalToolInvoker, ToolCallWorkflow

    fakes = Fakes()
    agent = Worker(env.client, task_queue=AGENT_QUEUE, workflows=[ToolCallWorkflow])
    gateway = Worker(env.client, task_queue=GATEWAY_QUEUE, activities=fakes.gateway_activities())
    request = ToolRequest(
        org_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        agent_run_id=uuid.uuid4(),
        tool_name="artifacts.read",
        arguments={"artifact_id": "x"},
        call_id="a1-t1",
    )
    async with agent, gateway:
        invoker = TemporalToolInvoker(env.client)
        first = await invoker.invoke(request)
        second = await invoker.invoke(request)
    assert first == second and first.status == "succeeded"
    assert len(fakes.tool_calls) == 1
