"""Test doubles for the model layer (no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aieos.agents.loop import SUBMIT_TOOL
from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ChatResponse,
    ToolCall,
    ToolResultsMessage,
    UserMessage,
)
from aieos.providers.metering import BudgetExceededError, BudgetStatus, MeteringContext
from aieos.providers.pricing import TokenUsage


def response(
    text: str = "ok",
    *,
    inp: int = 1000,
    cached: int = 0,
    cache_write: int = 0,
    out: int = 200,
    rid: str = "req-1",
    calls: tuple[ToolCall, ...] = (),
    stop: str | None = "end_turn",
) -> ChatResponse:
    return ChatResponse(
        message=AssistantMessage(text=text, tool_calls=calls, provider="fake"),
        usage=TokenUsage(inp, cached, out, cache_write),
        request_id=rid,
        stop_reason=stop,
    )


class FakeModel:
    """Returns queued results in order; an Exception in the queue is raised.
    Records every request it receives."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0
        self.requests: list[ChatRequest] = []

    @property
    def instructions(self) -> list[str]:
        return [r.system for r in self.requests]

    @property
    def inputs(self) -> list[ChatRequest]:
        return self.requests

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        self.requests.append(request)
        result = self.results.pop(0) if self.results else response(rid=f"auto-{self.calls}")
        if isinstance(result, Exception):
            raise result
        return result


def text_of(request: ChatRequest) -> str:
    """All text a model would see in a request's conversation (user text and tool results)."""
    parts: list[str] = []
    for m in request.messages:
        if isinstance(m, (UserMessage, AssistantMessage)):
            parts.append(m.text)
        elif isinstance(m, ToolResultsMessage):
            parts.extend(r.content for r in m.results)
    return "\n".join(parts)


@dataclass
class MemoryRecorder:
    budget_usd: Decimal = Decimal("10")
    spent_usd: Decimal = Decimal("0")
    records: list[dict[str, Any]] = field(default_factory=list)

    async def check_budget(self, ctx: MeteringContext) -> BudgetStatus:
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceededError("exhausted")
        return BudgetStatus(self.spent_usd, self.budget_usd, 0, 10**9, self.spent_usd, 0)

    async def record(
        self,
        ctx: MeteringContext,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost_usd: Decimal,
        provider_request_id: str | None,
    ) -> None:
        self.spent_usd += cost_usd
        self.records.append(
            {"provider": provider, "model": model, "usage": usage, "cost": cost_usd, "request_id": provider_request_id}
        )


def function_call(name: str, arguments: dict[str, Any], call_id: str = "call_1", rid: str = "fc") -> ChatResponse:
    call = ToolCall(id=call_id, name=name.replace(".", "__"), arguments=arguments)
    return response("", inp=100, out=20, rid=f"{rid}-{call_id}", calls=(call,), stop="tool_use")


def json_output(payload: Any, rid: str = "out") -> ChatResponse:
    """The model's final answer: a submit_output call carrying the payload."""
    args = payload if isinstance(payload, dict) else {"value": payload}
    return function_call(SUBMIT_TOOL, json.loads(json.dumps(args)), call_id=f"submit-{rid}", rid=rid)


REQUEST = "On the Reports page, add an Export CSV button. Exports must respect report permissions."


def spec_payload(*, quote: str = "add an Export CSV button", blocking: bool = False) -> dict[str, Any]:
    return {
        "summary": "CSV export",
        "facts": ["Reports page exists"],
        "assumptions": [],
        "requirements": [
            {
                "id": "REQ-1",
                "statement": "Users can export the report as CSV",
                "priority": "must",
                "sources": [{"kind": "request", "ref": quote}],
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "requirement_ids": ["REQ-1"],
                "given": "a report",
                "when": "the user clicks Export CSV",
                "then": "a CSV file downloads",
            }
        ],
        "open_questions": [
            {
                "id": "Q-1",
                "question": "Which columns?",
                "blocking": blocking,
                "why_it_matters": "defines the file format",
            }
        ],
        "out_of_scope": [],
    }


def design_payload(ac_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "overview": "Add an export endpoint and button",
        "components": [
            {
                "name": "reports-api",
                "responsibility": "serve CSV",
                "change_type": "modified",
                "files_expected": ["app/reports.py"],
            }
        ],
        "interfaces": ["GET /reports/{id}.csv"],
        "data_model_changes": [],
        "decisions": [],
        "security_considerations": ["reuse report permission check"],
        "risks": [],
        "diagram_mermaid": None,
        "proposed_tasks": [
            {
                "key": "T-1",
                "title": "CSV endpoint",
                "description": "add endpoint",
                "acceptance_criteria_ids": ac_ids or ["AC-1"],
                "depends_on": [],
                "assigned_agent_key": "developer",
            }
        ],
    }
