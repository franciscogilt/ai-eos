"""`MeteredModel`: the only Model the agent runtime ever receives.

For each call: budget check -> try candidates in order (primary, then fallbacks),
skipping providers whose circuit is open -> record usage and cost.
Only transient provider errors fall through to the next candidate; a bad request
is a bug and is raised immediately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import openai
from agents.items import ModelResponse
from agents.models.interface import Model

from aieos.contracts.agent_contract import ModelRef
from aieos.core.logging import get_logger
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import Price
from aieos.providers.metering import MeteringContext, UsageRecorder
from aieos.providers.pricing import TokenUsage, cost_usd

log = get_logger(__name__)

TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)


class ModelUnavailableError(RuntimeError):
    """Every candidate failed transiently or had an open circuit. Retryable at the workflow level."""


@dataclass(frozen=True, slots=True)
class Candidate:
    ref: ModelRef
    model: Model
    price: Price
    breaker: CircuitBreaker


def usage_from(response: ModelResponse) -> TokenUsage:
    u = response.usage
    details = u.input_tokens_details
    cached = getattr(details, "cached_tokens", 0) or 0
    cache_write = getattr(details, "cache_write_tokens", 0) or 0
    return TokenUsage(
        input_tokens=u.input_tokens,
        cached_input_tokens=cached,
        output_tokens=u.output_tokens,
        cache_write_input_tokens=cache_write,
    )


class MeteredModel(Model):
    def __init__(self, candidates: list[Candidate], *, recorder: UsageRecorder, ctx: MeteringContext) -> None:
        if not candidates:
            raise ValueError("at least one candidate model is required")
        self._candidates = candidates
        self._recorder = recorder
        self._ctx = ctx

    async def get_response(  # mirrors the Agents SDK Model signature
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any = None,
        conversation_id: Any = None,
        prompt: Any = None,
    ) -> ModelResponse:
        await self._recorder.check_budget(self._ctx)  # raises BudgetExceededError
        last_error: Exception | None = None
        for c in self._candidates:
            if not c.breaker.allow():
                log.info("model.circuit_open", provider=c.ref.provider, model=c.ref.model)
                continue
            try:
                response = await c.model.get_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    tracing,
                    previous_response_id=previous_response_id,
                    conversation_id=conversation_id,
                    prompt=prompt,
                )
            except TRANSIENT_ERRORS as exc:
                c.breaker.record_failure()
                last_error = exc
                log.warning(
                    "model.transient_error", provider=c.ref.provider, model=c.ref.model, error=type(exc).__name__
                )
                continue
            c.breaker.record_success()
            usage = usage_from(response)
            cost = cost_usd(usage, c.price)
            await self._recorder.record(
                self._ctx,
                provider=c.ref.provider,
                model=c.ref.model,
                usage=usage,
                cost_usd=cost,
                provider_request_id=response.request_id or response.response_id,
            )
            log.info(
                "model.call",
                agent=self._ctx.agent_key,
                provider=c.ref.provider,
                model=c.ref.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=str(cost),
            )
            return response
        raise ModelUnavailableError(
            f"no model available for agent {self._ctx.agent_key}"
            + (f" (last error: {type(last_error).__name__})" if last_error else " (all circuits open)")
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError("streaming is disabled: metering needs complete responses")
