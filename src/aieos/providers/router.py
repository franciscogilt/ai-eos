"""ModelRouter: contract model config -> ordered, priced, circuit-protected candidates.

A run is pinned to the candidates its contract declares: nothing an agent or a prompt says
can switch it to another model."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable

from aieos.contracts.agent_contract import AgentContract, ModelConfig, ModelRef
from aieos.providers.adapters.anthropic import AnthropicChatModel
from aieos.providers.adapters.cli import DEFAULT_COMMANDS, CliChatModel
from aieos.providers.adapters.gemini import GeminiChatModel
from aieos.providers.adapters.openai_chat import OpenAIChatModel
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import ProviderConfig, RoutingConfig
from aieos.providers.llm import ChatModel
from aieos.providers.metering import MeteringContext, UsageRecorder
from aieos.providers.models import Candidate, MeteredModel

ModelFactory = Callable[[str, ProviderConfig, str], ChatModel]


class ModelNotConfiguredError(RuntimeError):
    pass


def http_model_factory(provider_name: str, cfg: ProviderConfig, model_name: str) -> ChatModel:
    """Build the adapter for a provider's protocol. Keys come from the environment only."""
    timeout = cfg.timeout_seconds
    if cfg.kind == "cli":
        assert cfg.cli is not None
        command = cfg.command or DEFAULT_COMMANDS[cfg.cli]
        if shutil.which(os.path.expanduser(command)) is None:
            raise ModelNotConfiguredError(f"provider {provider_name}: `{command}` is not installed or not on PATH")
        return CliChatModel(cfg.cli, model_name, command=command, extra_args=cfg.extra_args, timeout_seconds=timeout)
    api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    if cfg.api_key_env and not api_key:
        raise ModelNotConfiguredError(f"provider {provider_name}: environment variable {cfg.api_key_env} is not set")
    base_url = os.environ.get(cfg.base_url_env) if cfg.base_url_env else None
    if cfg.kind == "openai_compatible" and not base_url:
        raise ModelNotConfiguredError(f"provider {provider_name}: {cfg.base_url_env} is not set")
    if cfg.kind == "anthropic":
        assert api_key is not None
        return AnthropicChatModel(model_name, api_key=api_key, base_url=base_url, timeout_seconds=timeout)
    if cfg.kind == "gemini":
        assert api_key is not None
        return GeminiChatModel(model_name, api_key=api_key, base_url=base_url, timeout_seconds=timeout)
    return OpenAIChatModel(model_name, api_key=api_key, kind=cfg.kind, base_url=base_url, timeout_seconds=timeout)


class ModelRouter:
    def __init__(
        self,
        config: RoutingConfig,
        *,
        model_factory: ModelFactory = http_model_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._factory = model_factory
        self._clock = clock
        self._models: dict[tuple[str, str], ChatModel] = {}
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

    def _model(self, ref: ModelRef) -> ChatModel:
        key = (ref.provider, ref.model)
        if key not in self._models:
            self._models[key] = self._factory(ref.provider, self._config.providers[ref.provider], ref.model)
        return self._models[key]

    def model_for_run(self, contract: AgentContract, ctx: MeteringContext, recorder: UsageRecorder) -> MeteredModel:
        candidates = []
        for ref in self.resolve(contract.model):
            price = self._config.price_for(ref)
            assert price is not None  # guaranteed by resolve()
            send_temperature = self._config.providers[ref.provider].send_temperature
            candidates.append(Candidate(ref, self._model(ref), price, self.breaker(ref.provider), send_temperature))
        return MeteredModel(candidates, recorder=recorder, ctx=ctx)
