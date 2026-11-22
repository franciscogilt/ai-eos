"""The feature-delivery process, independent of Temporal.

`DeliveryProcess` holds the orchestration logic; all side effects go through `Runtime`.
In production `TemporalRuntime` (workflows.py) runs them as activities and durable
timers, so this code must stay deterministic: no clocks, randomness or I/O here.
Tests drive the same code with an in-memory runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.orchestration.models import (
    AgentStepRequest,
    AgentStepResult,
    ApprovalDecisionSignal,
    ClarificationAnswers,
    DeliveryOutcome,
    DeliveryStatus,
    GateEvent,
    PlanEvent,
    PlannedTaskRef,
    ProgressEnvelope,
    StartDeliveryInput,
    StepEvent,
    TaskEvent,
    WorkflowStatusEvent,
)


class Runtime(Protocol):
    async def record(self, envelope: ProgressEnvelope) -> None: ...
    async def run_agent_step(self, request: AgentStepRequest) -> AgentStepResult: ...
    async def call_tool(self, request: ToolRequest) -> ToolResult: ...
    async def supervisor_run(self, org_id: UUID, workflow_id: UUID) -> UUID: ...
    async def approval_status(self, org_id: UUID, approval_id: UUID) -> str: ...
    async def wait(self, condition: Callable[[], bool], timeout: timedelta) -> bool: ...
    async def cleanup(self, org_id: UUID, workflow_id: UUID) -> None: ...


class StepFailedError(Exception):
    """A step failed for a reason that ends the workflow (budget, policy, rejected review...)."""


class CancelledByUserError(Exception):
    pass


def topological_order(tasks: list[PlannedTaskRef]) -> list[PlannedTaskRef]:
    """Deterministic Kahn ordering (ties broken by plan order). Raises on unknown deps or cycles."""
    by_key = {t.key: t for t in tasks}
    if len(by_key) != len(tasks):
        raise StepFailedError("plan has duplicate task keys")
    for t in tasks:
        unknown = set(t.depends_on) - by_key.keys()
        if unknown:
            raise StepFailedError(f"task {t.key} depends on unknown tasks {sorted(unknown)}")
    done: set[str] = set()
    ordered: list[PlannedTaskRef] = []
    while len(ordered) < len(tasks):
        ready = [t for t in tasks if t.key not in done and set(t.depends_on) <= done]
        if not ready:
            raise StepFailedError("plan dependencies contain a cycle")
        ordered.append(ready[0])
        done.add(ready[0].key)
    return ordered


class DeliveryProcess:
    def __init__(self, runtime: Runtime, start: StartDeliveryInput) -> None:
        self._rt = runtime
        self._in = start
        self._phase = "starting"
        self._step: str | None = None
        self._sequence = 0
        self._questions: list[dict[str, Any]] = []
        self._answers: dict[str, str] | None = None
        self._decisions: dict[UUID, str] = {}
        self._pending_approval: UUID | None = None
        self._cancel_reason: str | None = None
        self._supervisor_run: UUID | None = None

    # ------------------------------------------------------------------ signals / query
    def answer(self, answers: ClarificationAnswers) -> None:
        self._answers = dict(answers.answers)

    def decide(self, signal: ApprovalDecisionSignal) -> None:
        self._decisions[signal.approval_id] = signal.decision

    def request_cancel(self, reason: str) -> None:
        self._cancel_reason = reason or "cancelled by user"

    def status(self) -> DeliveryStatus:
        return DeliveryStatus(
            phase=self._phase,
            current_step=self._step,
            open_questions=list(self._questions) if self._phase == "awaiting_input" else [],
            pending_approval_id=self._pending_approval,
            cancel_requested=self._cancel_reason is not None,
        )

    # ------------------------------------------------------------------ main
    async def run(self) -> DeliveryOutcome:
        org, wf = self._in.org_id, self._in.workflow_id
        try:
            await self._workflow_status("running")
            self._supervisor_run = await self._rt.supervisor_run(org, wf)
            spec = await self._requirements()
            design = await self._agent_step("architecture", "architecture", {"requirements": spec.output})
            tasks = topological_order([PlannedTaskRef.model_validate(t) for t in design.output.get("tasks", [])])
            if not tasks:
                raise StepFailedError("architecture produced no tasks")
            await self._record(PlanEvent(org_id=org, workflow_id=wf, tasks=tasks))
            for task in tasks:
                await self._deliver_task(task, spec, design)
            await self._record(
                GateEvent(
                    org_id=org,
                    workflow_id=wf,
                    gate_key="all_tasks_reviewed",
                    result="passed",
                    details={"tasks": [t.key for t in tasks]},
                )
            )
            branch = f"aieos/{wf}"
            pr = await self._tool(
                "open-pull-request",
                "github.create_pull_request",
                {
                    "repository_id": str(self._in.repository_id),
                    "head": branch,
                    "title": self._title(),
                    "task_keys": [t.key for t in tasks],
                },
            )
            await self._tool(
                "merge-pull-request",
                "github.merge_pull_request",
                {"repository_id": str(self._in.repository_id), "pull_request": (pr.result or {}).get("number")},
            )
            await self._rt.cleanup(org, wf)
            await self._workflow_status("succeeded")
            return DeliveryOutcome(status="succeeded", reason="merged", pull_request=pr.result)
        except CancelledByUserError:
            return await self._finish_cancelled(self._cancel_reason or "cancelled")
        except StepFailedError as exc:
            if self._step:
                await self._step_event(self._step, "failed", error=str(exc))
            await self._workflow_status("failed", reason=str(exc))
            await self._rt.cleanup(org, wf)
            return DeliveryOutcome(status="failed", reason=str(exc))

    async def _finish_cancelled(self, reason: str) -> DeliveryOutcome:
        if self._step:
            await self._step_event(self._step, "cancelled")
        await self._rt.cleanup(self._in.org_id, self._in.workflow_id)
        await self._workflow_status("cancelled", reason=reason)
        return DeliveryOutcome(status="cancelled", reason=reason)

    # ------------------------------------------------------------------ phases
    async def _requirements(self) -> AgentStepResult:
        clarifications: dict[str, str] = {}
        limits = self._in.limits
        for round_no in range(limits.max_clarification_rounds + 1):
            spec = await self._agent_step(
                "requirements",
                "requirements",
                {"request": self._in.request, "clarifications": clarifications},
                attempt=round_no + 1,
            )
            blocking = [q for q in spec.output.get("open_questions", []) if q.get("blocking")]
            if not blocking:
                return spec
            if round_no == limits.max_clarification_rounds:
                break
            self._questions, self._answers = blocking, None
            await self._step_event("requirements", "awaiting_input", attempt=round_no + 1)
            await self._workflow_status("awaiting_input")
            answered = await self._rt.wait(
                lambda: self._answers is not None or self._cancel_reason is not None,
                timedelta(hours=limits.clarification_timeout_hours),
            )
            self._check_cancel()
            if not answered or self._answers is None:
                raise StepFailedError("clarification questions were not answered in time")
            clarifications.update(self._answers)
            self._questions = []
            await self._workflow_status("running")
        raise StepFailedError(
            f"requirements still blocked after {limits.max_clarification_rounds} clarification rounds"
        )

    async def _deliver_task(self, task: PlannedTaskRef, spec: AgentStepResult, design: AgentStepResult) -> None:
        org, wf = self._in.org_id, self._in.workflow_id
        feedback: list[Any] = []
        for attempt in range(1, self._in.limits.max_rework_rounds + 2):
            await self._record(
                TaskEvent(org_id=org, workflow_id=wf, task_key=task.key, status="running", attempt=attempt)
            )
            dev = await self._agent_step(
                f"develop:{task.key}",
                "developer",
                {"task": task.model_dump(mode="json"), "feedback": feedback},
                attempt=attempt,
            )
            if dev.status == "blocked":
                raise StepFailedError(f"task {task.key} blocked: {dev.summary}")
            await self._record(
                TaskEvent(org_id=org, workflow_id=wf, task_key=task.key, status="in_review", attempt=attempt)
            )
            review = await self._agent_step(
                f"review:{task.key}",
                "qa_security",
                {
                    "task": task.model_dump(mode="json"),
                    "patch": dev.output,
                    "author_agent_run_id": str(dev.agent_run_id),
                },
                attempt=attempt,
            )
            approved = review.output.get("verdict") == "approve"
            await self._record(
                GateEvent(
                    org_id=org,
                    workflow_id=wf,
                    gate_key="independent_review",
                    task_key=task.key,
                    result="passed" if approved else "failed",
                    details={"attempt": attempt, "summary": review.summary},
                )
            )
            if approved:
                await self._record(
                    TaskEvent(org_id=org, workflow_id=wf, task_key=task.key, status="succeeded", attempt=attempt)
                )
                return
            feedback = list(review.output.get("findings", []))
        await self._record(
            TaskEvent(
                org_id=org,
                workflow_id=wf,
                task_key=task.key,
                status="failed",
                attempt=self._in.limits.max_rework_rounds + 1,
            )
        )
        raise StepFailedError(f"task {task.key} not approved after {self._in.limits.max_rework_rounds} rework rounds")

    # ------------------------------------------------------------------ building blocks
    async def _agent_step(
        self, step_key: str, agent_key: str, inputs: dict[str, Any], *, attempt: int = 1
    ) -> AgentStepResult:
        self._check_cancel()
        self._step = step_key
        await self._step_event(step_key, "running", attempt=attempt)
        result = await self._rt.run_agent_step(
            AgentStepRequest(
                org_id=self._in.org_id,
                workflow_id=self._in.workflow_id,
                agent_key=agent_key,
                step_key=step_key,
                attempt=attempt,
                inputs=inputs,
            )
        )
        await self._step_event(step_key, "succeeded", attempt=attempt)
        self._check_cancel()
        return result

    async def _tool(self, call_id: str, tool: str, arguments: dict[str, Any]) -> ToolResult:
        """Call a tool through the gateway; wait for a human decision when it asks for approval."""
        assert self._supervisor_run is not None
        self._check_cancel()
        self._step = call_id
        await self._step_event(call_id, "running")
        request = ToolRequest(
            org_id=self._in.org_id,
            workflow_id=self._in.workflow_id,
            agent_run_id=self._supervisor_run,
            tool_name=tool,
            arguments=arguments,
            call_id=call_id,
        )
        while True:
            result = await self._rt.call_tool(request)
            if result.status == "succeeded":
                self._pending_approval = None
                await self._step_event(call_id, "succeeded")
                if self._phase != "running":
                    await self._workflow_status("running")
                return result
            if result.status in ("denied", "failed"):
                raise StepFailedError(f"{tool} {result.status}: {result.reason}")
            assert result.approval_id is not None
            decision = await self._await_approval(result.approval_id, call_id)
            if decision != "approved":
                raise StepFailedError(f"{tool} approval {decision}")
            # Approved: call again with the same call_id; the gateway now finds the approval and executes.

    async def _await_approval(self, approval_id: UUID, step_key: str) -> str:
        self._pending_approval = approval_id
        await self._step_event(step_key, "awaiting_approval")
        await self._workflow_status("awaiting_approval")
        poll = timedelta(minutes=self._in.limits.approval_poll_minutes)
        while True:
            signalled = await self._rt.wait(
                lambda: approval_id in self._decisions or self._cancel_reason is not None, poll
            )
            self._check_cancel()
            if signalled:
                return self._decisions[approval_id]
            # Backstop for a lost signal: the database is the source of truth.
            status = await self._rt.approval_status(self._in.org_id, approval_id)
            if status != "pending":
                return status

    def _check_cancel(self) -> None:
        if self._cancel_reason is not None:
            raise CancelledByUserError(self._cancel_reason)

    def _title(self) -> str:
        first_line = self._in.request.strip().splitlines()[0]
        return first_line[:120]

    async def _record(self, event: Any) -> None:
        await self._rt.record(ProgressEnvelope(event=event))

    async def _workflow_status(self, status: str, reason: str | None = None) -> None:
        self._phase = status
        await self._record(
            WorkflowStatusEvent(org_id=self._in.org_id, workflow_id=self._in.workflow_id, status=status, reason=reason)
        )

    async def _step_event(self, step_key: str, status: str, *, attempt: int = 1, error: str | None = None) -> None:
        if status == "running":
            self._sequence += 1
        await self._record(
            StepEvent(
                org_id=self._in.org_id,
                workflow_id=self._in.workflow_id,
                step_key=step_key,
                sequence=self._sequence,
                attempt=attempt,
                status=status,
                error=error,
            )
        )
