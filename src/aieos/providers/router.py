"""ModelRouter: contract model config -> ordered, priced, circuit-protected candidates."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from agents.models.interface import Model, ModelProvider
from openai import AsyncOpenAI

from aieos.contracts.agent_contract import AgentContract, ModelConfig, ModelRef
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import ProviderConfig, RoutingConfig
from aieos.providers.metering import MeteringContext, UsageRecorder
from aieos.providers.models import Candidate, MeteredModel

ModelFactory = Callable[[str, ProviderConfig, str], Model]


class ModelNotConfiguredError(RuntimeError):
    pass


def openai_model_factory(provider_name: str, cfg: ProviderConfig, model_name: str) -> Model:
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise ModelNotConfiguredError(f"provider {provider_name}: environment variable {cfg.api_key_env} is not set")
    base_url = os.environ.get(cfg.base_url_env) if cfg.base_url_env else None
    if cfg.kind == "openai_compatible" and not base_url:
        raise ModelNotConfiguredError(f"provider {provider_name}: {cfg.base_url_env} is not set")
    # Retries are ours (fallback + circuit breaker) and Temporal's, not the client's.
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=cfg.timeout_seconds, max_retries=0)
    if cfg.kind == "openai":
        return OpenAIResponsesModel(model_name, client)
    return OpenAIChatCompletionsModel(model_name, client)


class ModelRouter:
    def __init__(
        self,
        config: RoutingConfig,
        *,
        model_factory: ModelFactory = openai_model_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._factory = model_factory
        self._clock = clock
        self._models: dict[tuple[str, str], Model] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def resolve(self, model_cfg: ModelConfig) -> list[ModelRef]:
        refs: list[ModelRef] = []
        for declared in (model_cfg.primary, *model_cfg.fallbacks):
            ref = ModelRef(
                provider=declared.provider,
                model=self._config.model_id(declared.provider, model_cfg.profile, declared.model),
            )
            if ref in refs:
                continue
            if ref.provider not in self._config.providers:
                raise ModelNotConfiguredError(f"unknown provider {ref.provider!r}")
            if ref.model.startswith("REPLACE_"):
                raise ModelNotConfiguredError(
                    f"model for profile {model_cfg.profile} on {ref.provider} is a placeholder; "
                    "set it under `models` in model-routing.yaml"
                )
            if self._config.price_for(ref) is None:
                raise ModelNotConfiguredError(
                    f"no price for {ref.provider}/{ref.model}; unpriced models cannot be routed"
                )
            refs.append(ref)
        return refs

    def breaker(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(
                failure_threshold=self._config.circuit_breaker_failures,
                reset_after_seconds=self._config.circuit_breaker_reset_seconds,
                clock=self._clock,
            )
        return self._breakers[provider]

    def _model(self, ref: ModelRef) -> Model:
        key = (ref.provider, ref.model)
        if key not in self._models:
            self._models[key] = self._factory(ref.provider, self._config.providers[ref.provider], ref.model)
        return self._models[key]

    def model_for_run(self, contract: AgentContract, ctx: MeteringContext, recorder: UsageRecorder) -> MeteredModel:
        candidates = []
        for ref in self.resolve(contract.model):
            price = self._config.price_for(ref)
            assert price is not None  # guaranteed by resolve()
            candidates.append(Candidate(ref, self._model(ref), price, self.breaker(ref.provider)))
        return MeteredModel(candidates, recorder=recorder, ctx=ctx)

    def provider_for_run(
        self, contract: AgentContract, ctx: MeteringContext, recorder: UsageRecorder
    ) -> RunModelProvider:
        return RunModelProvider(self.model_for_run(contract, ctx, recorder), profile=contract.model.profile)


class RunModelProvider(ModelProvider):
    """Agents SDK ModelProvider for one agent run. The model is fixed by the contract:
    an agent definition or prompt cannot switch to another model by name."""

    def __init__(self, model: MeteredModel, *, profile: str) -> None:
        self._model = model
        self._profile = profile

    def get_model(self, model_name: str | None) -> Model:
        if model_name not in (None, "", self._profile):
            raise ModelNotConfiguredError(f"model {model_name!r} requested; this run is pinned to {self._profile!r}")
        return self._model
