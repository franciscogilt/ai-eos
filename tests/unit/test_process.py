"""DeliveryProcess driven by an in-memory runtime: the orchestration logic without Temporal."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.orchestration.models import (
    AgentStepRequest,
    AgentStepResult,
    ApprovalDecisionSignal,
    ClarificationAnswers,
    DeliveryLimits,
    PlannedTaskRef,
    ProgressEnvelope,
    StartDeliveryInput,
)
from aieos.orchestration.process import DeliveryProcess, StepFailedError, topological_order

TASKS = [{"key": "T-2", "title": "API", "depends_on": ["T-1"]}, {"key": "T-1", "title": "Schema"}]


class FakeRuntime:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.steps: list[AgentStepRequest] = []
        self.tool_calls: list[ToolRequest] = []
        self.agent: dict[str, list[Any]] = {}
        self.tools: dict[str, list[ToolResult]] = {}
        self.on_wait: Callable[[timedelta], None] = lambda _t: None
        self.approval_db: dict[uuid.UUID, str] = {}
        self.cleaned = False

    async def record(self, envelope: ProgressEnvelope) -> None:
        self.events.append(envelope.event)

    async def run_agent_step(self, request: AgentStepRequest) -> AgentStepResult:
        self.steps.append(request)
        queue = self.agent.get(request.agent_key, [])
        result = queue.pop(0) if queue else default_result(request.agent_key)
        if isinstance(result, Exception):
            raise result
        return result.model_copy(update={"agent_run_id": uuid.uuid4()})

    async def call_tool(self, request: ToolRequest) -> ToolResult:
        self.tool_calls.append(request)
        queue = self.tools.get(request.tool_name, [])
        return queue.pop(0) if queue else ToolResult(status="succeeded", reason="ok", result={"number": 7})

    async def supervisor_run(self, org_id: uuid.UUID, workflow_id: uuid.UUID) -> uuid.UUID:
        return uuid.UUID(int=1)

    async def approval_status(self, org_id: uuid.UUID, approval_id: uuid.UUID) -> str:
        return self.approval_db.get(approval_id, "pending")

    async def wait(self, condition: Callable[[], bool], timeout: timedelta) -> bool:
        self.on_wait(timeout)
        await asyncio.sleep(0)
        return condition()

    async def cleanup(self, org_id: uuid.UUID, workflow_id: uuid.UUID) -> None:
        self.cleaned = True

    def statuses(self) -> list[str]:
        return [e.status for e in self.events if e.kind == "workflow_status"]


def default_result(agent: str) -> AgentStepResult:
    output: dict[str, Any] = {
        "requirements": {"open_questions": []},
        "architecture": {"tasks": TASKS},
        "developer": {"patch_artifact_id": str(uuid.uuid4())},
        "qa_security": {"verdict": "approve"},
    }[agent]
    return AgentStepResult(status="completed", summary=f"{agent} done", output=output)


def start(**limits: Any) -> StartDeliveryInput:
    return StartDeliveryInput(
        org_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        request="Add CSV export\nDetails...",
        requested_by=uuid.uuid4(),
        limits=DeliveryLimits(**limits),
    )


def approval_needed(approval_id: uuid.UUID) -> ToolResult:
    return ToolResult(status="awaiting_approval", reason="human approval required", approval_id=approval_id)


def run(rt: FakeRuntime, inp: StartDeliveryInput | None = None) -> tuple[DeliveryProcess, Any]:
    process = DeliveryProcess(rt, inp or start())
    return process, asyncio.run(process.run())


def test_happy_path_with_merge_approval():
    rt = FakeRuntime()
    approval = uuid.uuid4()
    rt.tools["github.merge_pull_request"] = [approval_needed(approval)]
    holder: dict[str, DeliveryProcess] = {}
    rt.on_wait = lambda _t: holder["p"].decide(ApprovalDecisionSignal(approval_id=approval, decision="approved"))
    process = DeliveryProcess(rt, start())
    holder["p"] = process
    outcome = asyncio.run(process.run())

    assert outcome.status == "succeeded" and outcome.pull_request == {"number": 7}
    assert rt.statuses() == ["running", "awaiting_approval", "running", "succeeded"]
    assert [s.step_key for s in rt.steps] == [
        "requirements",
        "architecture",
        "develop:T-1",
        "review:T-1",
        "develop:T-2",
        "review:T-2",
    ]  # dependency order
    merge_calls = [c for c in rt.tool_calls if c.tool_name == "github.merge_pull_request"]
    assert len(merge_calls) == 2 and merge_calls[0].call_id == merge_calls[1].call_id  # same idempotency key
    assert merge_calls[0].arguments["pull_request"] == 7
    plan = next(e for e in rt.events if e.kind == "plan")
    assert [t.key for t in plan.tasks] == ["T-1", "T-2"]


def test_clarification_loop_passes_answers_back():
    rt = FakeRuntime()
    rt.agent["requirements"] = [
        AgentStepResult(
            status="completed",
            summary="blocked",
            output={"open_questions": [{"id": "Q-1", "question": "Which format?", "blocking": True}]},
        ),
        default_result("requirements"),
    ]
    holder: dict[str, DeliveryProcess] = {}

    def answer(_t: timedelta) -> None:
        assert holder["p"].status().open_questions[0]["id"] == "Q-1"
        holder["p"].answer(ClarificationAnswers(answers={"Q-1": "CSV"}))

    rt.on_wait = answer
    process = DeliveryProcess(rt, start())
    holder["p"] = process
    assert asyncio.run(process.run()).status == "succeeded"
    req_steps = [s for s in rt.steps if s.agent_key == "requirements"]
    assert [s.attempt for s in req_steps] == [1, 2]
    assert req_steps[1].inputs["clarifications"] == {"Q-1": "CSV"}
    assert rt.statuses()[:3] == ["running", "awaiting_input", "running"]


def test_unanswered_clarification_fails_and_cleans_up():
    rt = FakeRuntime()
    rt.agent["requirements"] = [
        AgentStepResult(status="completed", summary="b", output={"open_questions": [{"id": "Q-1", "blocking": True}]})
    ]
    _, outcome = run(rt)
    assert outcome.status == "failed" and "not answered" in outcome.reason and rt.cleaned


def test_non_blocking_questions_do_not_pause():
    rt = FakeRuntime()
    rt.agent["requirements"] = [
        AgentStepResult(status="completed", summary="ok", output={"open_questions": [{"id": "Q-1", "blocking": False}]})
    ]
    _, outcome = run(rt)
    assert outcome.status == "succeeded" and "awaiting_input" not in rt.statuses()


def test_rework_passes_findings_to_developer():
    rt = FakeRuntime()
    findings = [{"severity": "high", "title": "SQL injection"}]
    rt.agent["qa_security"] = [
        AgentStepResult(
            status="completed", summary="changes", output={"verdict": "request_changes", "findings": findings}
        )
    ]
    _, outcome = run(rt)
    assert outcome.status == "succeeded"
    dev_t1 = [s for s in rt.steps if s.step_key == "develop:T-1"]
    assert len(dev_t1) == 2 and dev_t1[1].inputs["feedback"] == findings
    gates = [e for e in rt.events if e.kind == "gate" and e.task_key == "T-1"]
    assert [g.result for g in gates] == ["failed", "passed"]


def test_rework_exhausted_fails():
    rt = FakeRuntime()
    reject = AgentStepResult(status="completed", summary="no", output={"verdict": "request_changes"})
    rt.agent["qa_security"] = [reject] * 3
    _, outcome = run(rt, start(max_rework_rounds=2))
    assert outcome.status == "failed" and "not approved after 2 rework rounds" in outcome.reason
    assert not any(c.tool_name.startswith("github.") for c in rt.tool_calls)


def test_blocked_developer_fails():
    rt = FakeRuntime()
    rt.agent["developer"] = [AgentStepResult(status="blocked", summary="spec is contradictory")]
    _, outcome = run(rt)
    assert outcome.status == "failed" and "contradictory" in outcome.reason


def test_rejected_approval_fails_without_merging():
    rt = FakeRuntime()
    approval = uuid.uuid4()
    rt.tools["github.merge_pull_request"] = [approval_needed(approval)]
    holder: dict[str, DeliveryProcess] = {}
    rt.on_wait = lambda _t: holder["p"].decide(ApprovalDecisionSignal(approval_id=approval, decision="rejected"))
    holder["p"] = process = DeliveryProcess(rt, start())
    outcome = asyncio.run(process.run())
    assert outcome.status == "failed" and "approval rejected" in outcome.reason
    assert len([c for c in rt.tool_calls if c.tool_name == "github.merge_pull_request"]) == 1


def test_lost_signal_recovered_by_polling():
    rt = FakeRuntime()
    approval = uuid.uuid4()
    rt.tools["github.merge_pull_request"] = [approval_needed(approval)]
    polls = {"n": 0}

    def poll(_t: timedelta) -> None:  # no signal ever arrives; the DB flips to approved on the 3rd poll
        polls["n"] += 1
        if polls["n"] == 3:
            rt.approval_db[approval] = "approved"

    rt.on_wait = poll
    _, outcome = run(rt, start(approval_poll_minutes=5))
    assert outcome.status == "succeeded" and polls["n"] == 3


def test_expired_approval_fails():
    rt = FakeRuntime()
    approval = uuid.uuid4()
    rt.tools["github.create_pull_request"] = [approval_needed(approval)]
    rt.approval_db[approval] = "expired"
    _, outcome = run(rt)
    assert outcome.status == "failed" and "approval expired" in outcome.reason


def test_cancel_while_waiting_for_approval():
    rt = FakeRuntime()
    approval = uuid.uuid4()
    rt.tools["github.create_pull_request"] = [approval_needed(approval)]
    holder: dict[str, DeliveryProcess] = {}
    rt.on_wait = lambda _t: holder["p"].request_cancel("not needed anymore")
    holder["p"] = process = DeliveryProcess(rt, start())
    outcome = asyncio.run(process.run())
    assert outcome.status == "cancelled" and outcome.reason == "not needed anymore"
    assert rt.cleaned and rt.statuses()[-1] == "cancelled"


def test_denied_tool_fails():
    rt = FakeRuntime()
    rt.tools["github.create_pull_request"] = [ToolResult(status="denied", reason="not granted")]
    _, outcome = run(rt)
    assert outcome.status == "failed" and "denied: not granted" in outcome.reason


def test_step_failure_from_runtime_fails_workflow():
    rt = FakeRuntime()
    rt.agent["architecture"] = [StepFailedError("architecture step failed: BudgetExceeded")]
    _, outcome = run(rt)
    assert outcome.status == "failed" and "BudgetExceeded" in outcome.reason and rt.cleaned
    failed_steps = [e for e in rt.events if e.kind == "step" and e.status == "failed"]
    assert failed_steps[-1].step_key == "architecture"


def test_empty_plan_fails():
    rt = FakeRuntime()
    rt.agent["architecture"] = [AgentStepResult(status="completed", summary="", output={"tasks": []})]
    assert run(rt)[1].status == "failed"


def test_topological_order_rules():
    t = [PlannedTaskRef(key="T-1", title="a", depends_on=["T-2"]), PlannedTaskRef(key="T-2", title="b")]
    assert [x.key for x in topological_order(t)] == ["T-2", "T-1"]
    with pytest.raises(StepFailedError, match="cycle"):
        topological_order(
            [PlannedTaskRef(key="A", title="a", depends_on=["B"]), PlannedTaskRef(key="B", title="b", depends_on=["A"])]
        )
    with pytest.raises(StepFailedError, match="unknown"):
        topological_order([PlannedTaskRef(key="A", title="a", depends_on=["Z"])])
    with pytest.raises(StepFailedError, match="duplicate"):
        topological_order([PlannedTaskRef(key="A", title="a"), PlannedTaskRef(key="A", title="b")])
