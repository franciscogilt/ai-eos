"""What a step preparer hands the runner, and the tool access a preparer gets.

A preparer turns the orchestrator's inputs into the agent's first message, its output model,
the checks its output must pass, and the step output. Platform checks (tests, patch export,
scans) run through the gateway like any other tool call, under the step's agent run, so they
are authorized, recorded and audited the same way.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from aieos.agents.factory import RunScope, ToolInvoker
from aieos.contracts.tools import ToolRequest, ToolResult

MaybeAwaitable = Any  # T | Awaitable[T]


async def resolve(value: MaybeAwaitable) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class Prepared:
    message: str
    output_type: type[BaseModel]
    validate: Callable[[Any], MaybeAwaitable]  # -> list[str] of violations
    to_output: Callable[[Any], MaybeAwaitable]  # -> dict, the step output
    summary: Callable[[Any], str]
    artifact_kind: str
    status: Callable[[Any], str] = field(default=lambda _output: "completed")


class StepTools:
    """Platform tool calls for a step. Call ids are stable per attempt, so a retried activity
    finds the recorded result instead of running a command twice."""

    def __init__(self, invoker: ToolInvoker, scope: RunScope) -> None:
        self._invoker = invoker
        self._scope = scope

    async def call(self, tool: str, arguments: dict[str, Any], call_id: str) -> ToolResult:
        return await self._invoker.invoke(
            ToolRequest(
                org_id=self._scope.org_id,
                workflow_id=self._scope.workflow_id,
                agent_run_id=self._scope.agent_run_id,
                tool_name=tool,
                arguments=arguments,
                call_id=call_id,
            )
        )


AsyncPreparer = Callable[[dict[str, Any], RunScope, StepTools], Awaitable[Prepared]]
