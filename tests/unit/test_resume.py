"""Crash-safe agent steps: a retried step resumes from its checkpoint instead of starting over.

A "crash" here is an exception raised mid-run (as a dying worker would leave things); the second run
gets the saved state, as a retried activity would.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from tests.fakes import FakeModel, function_call, json_output, spec_payload

from aieos.agents.factory import RunScope, build_agent, run_with_retries
from aieos.agents.loop import AgentDefinition, run_agent
from aieos.contracts.io import RequirementsSpec
from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.providers.llm import (
    AssistantMessage,
    ToolCall,
    ToolResultItem,
    ToolResultsMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
)


class Crash(RuntimeError):
    pass


class CountingInvoker:
    def __init__(self, *, crash_first: bool = False) -> None:
        self.requests: list[ToolRequest] = []
        self.crash_first = crash_first

    async def invoke(self, request: ToolRequest) -> ToolResult:
        if self.crash_first:
            self.crash_first = False
            raise Crash("worker died while the tool ran")
        self.requests.append(request)
        return ToolResult(status="succeeded", reason="ok", result={"text": "contents"})


class Store:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None
        self.saves = 0

    async def save(self, state: dict[str, Any]) -> None:
        self.state = state
        self.saves += 1


def test_messages_survive_serialization():
    messages = [
        UserMessage("hi"),
        AssistantMessage(
            "thinking",
            (ToolCall("c1", "repo__read_file", {"path": "a"}, None),),
            provider="gemini",
            provider_data={"parts": [{"functionCall": {"name": "x"}, "thoughtSignature": "SIG"}]},
        ),
        ToolResultsMessage((ToolResultItem("c1", "repo__read_file", "data", True),)),
    ]
    assert [message_from_dict(message_to_dict(m)) for m in messages] == messages


def test_loop_resumes_pending_tool_calls_without_calling_the_model():
    calls: list[dict[str, Any]] = []

    async def read(args: dict[str, Any]) -> tuple[str, bool]:
        calls.append(args)
        return "contents", False

    from aieos.agents.loop import AgentTool
    from aieos.providers.llm import ToolSpec

    tool = AgentTool(ToolSpec("repo__read_file", "read", {"type": "object"}), read)
    agent = AgentDefinition("t", "s", RequirementsSpec, tools={"repo__read_file": tool})
    history = [UserMessage("go"), AssistantMessage("", (ToolCall("c1", "repo__read_file", {"path": "a"}),))]
    fake = FakeModel(json_output(spec_payload()))
    asyncio.run(run_agent(agent, "go", model=fake, max_turns=5, history=history))
    assert calls == [{"path": "a"}] and fake.calls == 1  # the reply with the tool call was not re-requested
    assert "contents" in str(fake.requests[0].messages[-1])  # its result is in the next request


def scope() -> RunScope:
    return RunScope(org_id=uuid.uuid4(), workflow_id=uuid.uuid4(), agent_run_id=uuid.uuid4(), max_tool_calls=10)


def run(loaded, catalog, fake, invoker, sc, store, resume=None, validate=lambda _o: []):
    def make(attempt: int):
        sc.attempt = attempt
        return build_agent(
            loaded["requirements"], catalog=catalog, invoker=invoker, scope=sc, output_type=RequirementsSpec
        )

    return asyncio.run(
        run_with_retries(
            make,
            "Turn this into requirements",
            model=fake,
            output_type=RequirementsSpec,
            validate=validate,
            max_turns=8,
            timeout_seconds=30,
            max_retries=2,
            scope=sc,
            resume=resume,
            save=store.save,
        )
    )


@pytest.fixture(scope="module")
def loaded(definitions_dir, prompts_dir, catalog):
    from aieos.agents.registry import load_all

    return load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog)


def test_crash_after_tool_results_repeats_nothing(loaded, catalog):
    sc, store, invoker = scope(), Store(), CountingInvoker()
    first = FakeModel(function_call("artifacts.read", {"artifact_id": "x"}), Crash("worker died"))
    with pytest.raises(Crash):
        run(loaded, catalog, first, invoker, sc, store)
    saved = store.state
    assert saved is not None and [m["role"] for m in saved["messages"]] == ["user", "assistant", "tool"]
    assert saved["scope"]["tool_calls"] == 1

    second = FakeModel(json_output(spec_payload()))
    retry_scope = RunScope(
        org_id=sc.org_id, workflow_id=sc.workflow_id, agent_run_id=sc.agent_run_id, max_tool_calls=10
    )
    output, attempts = run(loaded, catalog, second, invoker, retry_scope, store, resume=saved)
    assert isinstance(output, RequirementsSpec) and attempts == 1
    assert second.calls == 1 and len(invoker.requests) == 1  # no model call or tool call repeated
    assert retry_scope.tool_calls == 1  # the counter continues, so the next call id would be a1-t2


def test_crash_while_a_tool_runs_retries_it_with_the_same_call_id(loaded, catalog):
    sc, store = scope(), Store()
    invoker = CountingInvoker(crash_first=True)
    first = FakeModel(function_call("artifacts.read", {"artifact_id": "x"}))
    with pytest.raises(Crash):
        run(loaded, catalog, first, invoker, sc, store)
    saved = store.state
    assert saved is not None and saved["messages"][-1]["role"] == "assistant"  # saved before the tool ran
    assert saved["scope"]["tool_calls"] == 0

    second = FakeModel(json_output(spec_payload()))
    retry_scope = RunScope(
        org_id=sc.org_id, workflow_id=sc.workflow_id, agent_run_id=sc.agent_run_id, max_tool_calls=10
    )
    run(loaded, catalog, second, invoker, retry_scope, store, resume=saved)
    assert [r.call_id for r in invoker.requests] == ["a1-t1"]  # the gateway would return the recorded result
    assert second.calls == 1


def test_resume_continues_a_later_grounding_attempt(loaded, catalog):
    sc, store, invoker = scope(), Store(), CountingInvoker()
    verdicts = iter([["requirement REQ-1 cites text not in the request"], []])
    first = FakeModel(json_output(spec_payload(), rid="1"), Crash("died during attempt 2"))
    with pytest.raises(Crash):
        run(loaded, catalog, first, invoker, sc, store, validate=lambda _o: next(verdicts))
    saved = store.state
    assert saved is not None and saved["attempt"] == 2 and saved["messages"] is None
    assert saved["feedback"] == ["requirement REQ-1 cites text not in the request"]

    second = FakeModel(json_output(spec_payload(), rid="2"))
    _, attempts = run(loaded, catalog, second, invoker, sc, store, resume=saved, validate=lambda _o: [])
    assert attempts == 2 and "cites text not in the request" in str(second.requests[0].messages[0])
