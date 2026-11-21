"""Test doubles for the model layer (no network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_usage import InputTokensDetails

from aieos.providers.metering import BudgetExceededError, BudgetStatus, MeteringContext
from aieos.providers.pricing import TokenUsage


def response(
    text: str = "ok", *, inp: int = 1000, cached: int = 0, cache_write: int = 0, out: int = 200, rid: str = "req-1"
) -> ModelResponse:
    message = ResponseOutputMessage(
        id="msg_1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )
    usage = Usage(
        requests=1,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
        input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=cache_write),
    )
    return ModelResponse(output=[message], usage=usage, response_id=f"resp-{rid}", request_id=rid)


class FakeModel(Model):
    """Returns queued results in order; an Exception in the queue is raised.
    Records the system instructions and input of every call."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0
        self.instructions: list[Any] = []
        self.inputs: list[Any] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        if args:
            self.instructions.append(args[0])
            self.inputs.append(args[1])
        result = self.results.pop(0) if self.results else response(rid=f"auto-{self.calls}")
        if isinstance(result, Exception):
            raise result
        return result

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


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


def function_call(name: str, arguments: dict[str, Any], call_id: str = "call_1", rid: str = "fc") -> ModelResponse:
    from openai.types.responses import ResponseFunctionToolCall

    item = ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=name.replace(".", "__"),
        arguments=__import__("json").dumps(arguments),
        id=f"fc_{call_id}",
        status="completed",
    )
    usage = Usage(
        requests=1,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        input_tokens_details=InputTokensDetails(cached_tokens=0, cache_write_tokens=0),
    )
    return ModelResponse(output=[item], usage=usage, response_id=f"resp-{rid}-{call_id}", request_id=f"{rid}-{call_id}")


def json_output(payload: Any, rid: str = "out") -> ModelResponse:
    import json

    return response(json.dumps(payload), rid=rid)


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
