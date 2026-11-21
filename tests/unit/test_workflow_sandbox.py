"""Checks Temporal's workflow sandbox accepts the workflow module, without a server.

The sandbox re-imports the workflow's module under restrictions (no I/O, clocks or
non-deterministic imports at import time). A violation here would make every workflow
task fail at runtime, so this runs everywhere, including where no Temporal server is available.
"""

from __future__ import annotations

import asyncio

from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from aieos.orchestration.workflows import FeatureDeliveryWorkflow


def test_workflow_passes_sandbox_validation():
    definition = workflow._Definition.must_from_class(FeatureDeliveryWorkflow)

    async def validate() -> None:  # the runner builds a throwaway instance on the running loop
        SandboxedWorkflowRunner().prepare_workflow(definition)

    asyncio.run(validate())


def test_workflow_interface():
    definition = workflow._Definition.must_from_class(FeatureDeliveryWorkflow)
    assert definition.name == "FeatureDeliveryWorkflow"
    assert set(definition.signals) == {"clarification_answered", "approval_decided", "cancel_requested"}
    assert set(definition.queries) == {"status"}


def test_tool_call_workflow_passes_sandbox_validation():
    from aieos.orchestration.tool_bridge import ToolCallWorkflow

    definition = workflow._Definition.must_from_class(ToolCallWorkflow)

    async def validate() -> None:
        SandboxedWorkflowRunner().prepare_workflow(definition)

    asyncio.run(validate())
