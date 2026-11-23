"""Spans for model calls, tool calls and errors: the right attributes, and no prompt/tool content."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from tests.fakes import FakeModel, MemoryRecorder, response
from tests.tracing import SpanCapture, all_attribute_text

from aieos.agents.loop import SUBMIT_TOOL, AgentDefinition, AgentTool, run_agent
from aieos.contracts.agent_contract import ModelRef
from aieos.observability.telemetry import span
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import Price
from aieos.providers.llm import (
    ChatRequest,
    ProviderRequestError,
    ProviderTransientError,
    ToolCall,
    ToolSpec,
    UserMessage,
)
from aieos.providers.metering import MeteringContext
from aieos.providers.models import Candidate, MeteredModel

PROMPT_CANARY = "canary-in-the-prompt"
ARG_CANARY = "canary-in-the-arguments"
RESULT_CANARY = "canary-in-the-result"
ERROR_CANARY = "canary-in-the-error"

PRICE = Price(input_per_mtok=Decimal("2"), cached_input_per_mtok=Decimal("0.5"), output_per_mtok=Decimal("8"))
CTX = MeteringContext(
    org_id=uuid4(),
    workflow_id=uuid4(),
    agent_run_id=uuid4(),
    agent_key="developer",
    run_max_cost_usd=Decimal("5"),
    run_max_tokens=1_000_000,
)


def metered(*models: FakeModel) -> MeteredModel:
    return MeteredModel(
        [
            Candidate(ModelRef(provider=f"p{i}", model=f"m{i}"), m, PRICE, CircuitBreaker())
            for i, m in enumerate(models)
        ],
        recorder=MemoryRecorder(),
        ctx=CTX,
    )


REQ = ChatRequest(system=PROMPT_CANARY, messages=(UserMessage(PROMPT_CANARY),))


def test_model_call_span_has_usage_and_cost_but_no_content():
    with SpanCapture() as cap:
        asyncio.run(metered(FakeModel(response(RESULT_CANARY, inp=1000, cached=400, out=200))).complete(REQ))
    (s,) = cap.named("chat ")
    a = s.attributes
    assert s.name == "chat m0" and a["gen_ai.provider.name"] == "p0" and a["gen_ai.request.model"] == "m0"
    assert a["gen_ai.usage.input_tokens"] == 1000 and a["gen_ai.usage.output_tokens"] == 200
    assert a["aieos.usage.cached_input_tokens"] == 400 and a["aieos.cost_usd"] == pytest.approx(0.003)
    assert a["gen_ai.agent.name"] == "developer" and a["aieos.agent_run_id"] == str(CTX.agent_run_id)
    assert a["aieos.model.fallback"] is False
    text = all_attribute_text(cap.spans)
    assert PROMPT_CANARY not in text and RESULT_CANARY not in text


def test_fallback_is_traced_and_errors_carry_type_only():
    with SpanCapture() as cap:
        model = metered(FakeModel(ProviderTransientError(f"p0: HTTP 503 {ERROR_CANARY}")), FakeModel(response()))
        asyncio.run(model.complete(REQ))
    first, second = cap.named("chat ")
    assert first.status.status_code is StatusCode.ERROR and first.attributes["error.type"] == "ProviderTransientError"
    assert second.attributes["aieos.model.fallback"] is True and second.status.status_code is not StatusCode.ERROR
    assert ERROR_CANARY not in all_attribute_text(cap.spans)


def test_raised_error_is_recorded_without_message():
    with SpanCapture() as cap, pytest.raises(ProviderRequestError):
        asyncio.run(metered(FakeModel(ProviderRequestError(f"HTTP 400 {ERROR_CANARY}"))).complete(REQ))
    (s,) = cap.named("chat ")
    assert s.attributes["error.type"] == "ProviderRequestError" and s.status.status_code is StatusCode.ERROR
    assert not s.events  # no exception event (it would hold the message and traceback)
    assert ERROR_CANARY not in all_attribute_text(cap.spans)


class Out(BaseModel):
    answer: str


def test_tool_call_spans_nest_under_the_caller_and_hold_no_arguments_or_results():
    async def lookup(args):
        assert args == {"q": ARG_CANARY}
        return RESULT_CANARY, False

    async def broken(args):
        return f"failed: {ERROR_CANARY}", True

    agent = AgentDefinition(
        name="developer",
        system=PROMPT_CANARY,
        output_type=Out,
        tools={
            "lookup": AgentTool(ToolSpec("lookup", "d", {"type": "object"}), lookup),
            "broken": AgentTool(ToolSpec("broken", "d", {"type": "object"}), broken),
        },
    )
    model = FakeModel(
        response(calls=(ToolCall("c1", "lookup", {"q": ARG_CANARY}), ToolCall("c2", "broken", {}))),
        response(calls=(ToolCall("c3", SUBMIT_TOOL, {"answer": "done"}),)),
    )
    with SpanCapture() as cap:

        async def go():
            with span("agent.step") as parent:
                await run_agent(agent, PROMPT_CANARY, model=model, max_turns=5)
                return parent.get_span_context()

        parent_ctx = asyncio.run(go())
    ok, bad = cap.named("execute_tool ")
    assert ok.name == "execute_tool lookup" and ok.attributes["gen_ai.tool.call.id"] == "c1"
    assert ok.attributes["aieos.tool.result_chars"] == len(RESULT_CANARY) and ok.attributes["aieos.turn"] == 1
    assert bad.status.status_code is StatusCode.ERROR and bad.attributes["error.type"] == "ToolError"
    assert ok.parent.span_id == parent_ctx.span_id and ok.context.trace_id == parent_ctx.trace_id
    text = all_attribute_text(cap.spans)
    for canary in (PROMPT_CANARY, ARG_CANARY, RESULT_CANARY, ERROR_CANARY):
        assert canary not in text


def test_no_exporter_configured_means_no_op():
    from aieos.observability.telemetry import setup_tracing

    assert setup_tracing(service_name="x", environment="test", otlp_endpoint=None) is None
