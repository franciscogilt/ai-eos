"""`MeteredModel`: the only model the agent loop ever receives.

For each call: budget check -> try candidates in order (primary, then fallbacks),
skipping providers whose circuit is open -> record usage and cost.
Only transient provider errors fall through to the next candidate; a rejected request
is a bug or a configuration error and is raised immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from aieos.contracts.agent_contract import ModelRef
from aieos.core.logging import get_logger
from aieos.observability.telemetry import mark_error, set_attributes, span
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import Price
from aieos.providers.llm import ChatModel, ChatRequest, ChatResponse, ProviderTransientError
from aieos.providers.metering import MeteringContext, UsageRecorder
from aieos.providers.pricing import cost_usd

log = get_logger(__name__)


class ModelUnavailableError(RuntimeError):
    """Every candidate failed transiently or had an open circuit. Retryable at the workflow level."""


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: ModelRef
    model: ChatModel
    price: Price
    breaker: CircuitBreaker
    send_temperature: bool = True


class MeteredModel:
    def __init__(self, candidates: list[Candidate], *, recorder: UsageRecorder, ctx: MeteringContext) -> None:
        if not candidates:
            raise ValueError("at least one candidate model is required")
        self._candidates = candidates
        self._recorder = recorder
        self._ctx = ctx

    @property
    def refs(self) -> list[ModelRef]:
        return [c.ref for c in self._candidates]

    async def complete(self, request: ChatRequest) -> ChatResponse:
        await self._recorder.check_budget(self._ctx)  # raises BudgetExceededError
        last_error: Exception | None = None
        for c in self._candidates:
            if not c.breaker.allow():
                log.info("model.circuit_open", provider=c.ref.provider, model=c.ref.model)
                continue
            req = request if c.send_temperature else replace(request, temperature=None)
            with span(
                f"chat {c.ref.model}",
                **{
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": c.ref.provider,
                    "gen_ai.request.model": c.ref.model,
                    "gen_ai.agent.name": self._ctx.agent_key,
                    "aieos.agent_run_id": self._ctx.agent_run_id,
                    "aieos.workflow_id": self._ctx.workflow_id,
                    "aieos.org_id": self._ctx.org_id,
                    "aieos.model.fallback": c is not self._candidates[0],
                    "aieos.request.messages": len(req.messages),
                    "aieos.request.tools": len(req.tools),
                },
            ) as current:
                try:
                    response = await c.model.complete(req)
                except ProviderTransientError as exc:
                    c.breaker.record_failure()
                    last_error = exc
                    mark_error(current, "ProviderTransientError")
                    log.warning("model.transient_error", provider=c.ref.provider, model=c.ref.model, error=str(exc))
                    continue
                c.breaker.record_success()
                cost = cost_usd(response.usage, c.price)
                set_attributes(
                    current,
                    **{
                        "gen_ai.usage.input_tokens": response.usage.input_tokens,
                        "gen_ai.usage.output_tokens": response.usage.output_tokens,
                        "aieos.usage.cached_input_tokens": response.usage.cached_input_tokens,
                        "aieos.cost_usd": float(cost),
                        "gen_ai.response.finish_reasons": response.stop_reason,
                        "aieos.response.tool_calls": len(response.message.tool_calls),
                    },
                )
            await self._recorder.record(
                self._ctx,
                provider=c.ref.provider,
                model=c.ref.model,
                usage=response.usage,
                cost_usd=cost,
                provider_request_id=response.request_id,
            )
            log.info(
                "model.call",
                agent=self._ctx.agent_key,
                provider=c.ref.provider,
                model=c.ref.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=str(cost),
            )
            return response
        raise ModelUnavailableError(
            f"no model available for agent {self._ctx.agent_key}"
            + (f" (last error: {last_error})" if last_error else " (all circuits open)")
        )
