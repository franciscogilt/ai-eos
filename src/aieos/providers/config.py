"""Model routing configuration (`engineering/model-routing.yaml`).

* providers: how to reach each provider. Keys are read from named environment
  variables at client construction, never stored in the file.
* models: model id per (provider, profile). The *provider* comes from the agent
  contract (so separation of duties stays in reviewed contracts); this file only
  says which model id that provider uses for each profile.
* pricing: USD per million tokens. A model without a price cannot be routed,
  so a budget can never be bypassed by an unpriced model.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aieos.contracts.agent_contract import ModelRef


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Strict):
    # Wire protocol. anthropic: Messages API. gemini: Gemini generateContent. openai: OpenAI Chat
    # Completions. openai_compatible: the same protocol at base_url (gateways, vLLM, Ollama, ...).
    # cli: a coding CLI already signed in on this machine (Claude Code, Gemini CLI, Codex), used
    # as a plain text model; see adapters/cli.py.
    kind: Literal["anthropic", "gemini", "openai", "openai_compatible", "cli"]
    # Required for the hosted APIs; optional for openai_compatible (Ollama needs none); unused for cli.
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    # Optional for the hosted vendors (overrides their public endpoint); required for openai_compatible.
    base_url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    # Some reasoning models reject a temperature parameter; set false for those.
    send_temperature: bool = True
    # kind: cli only.
    cli: Literal["claude_code", "gemini_cli", "codex_cli"] | None = None
    command: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./~-]+$")  # binary; default: the CLI's name
    extra_args: tuple[str, ...] = ()  # appended to the CLI's arguments, for flags your version needs

    @model_validator(mode="after")
    def _consistent(self) -> ProviderConfig:
        if self.kind == "openai_compatible" and not self.base_url_env:
            raise ValueError("openai_compatible providers need base_url_env")
        if self.kind in ("anthropic", "gemini", "openai") and not self.api_key_env:
            raise ValueError(f"{self.kind} providers need api_key_env")
        if (self.kind == "cli") != (self.cli is not None):
            raise ValueError("set `cli` exactly when kind is cli")
        return self


class Price(_Strict):
    input_per_mtok: Decimal = Field(ge=0)
    cached_input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    # Some providers bill prompt-cache writes separately; unset means "billed as normal input".
    cache_write_input_per_mtok: Decimal | None = Field(default=None, ge=0)


class RoutingConfig(_Strict):
    api_version: Literal["aieos/v1"]
    providers: dict[str, ProviderConfig]
    models: dict[str, dict[Literal["reasoning_high", "reasoning_standard", "fast"], str]] = {}
    # Key: "<provider>/<model>"
    pricing: dict[str, Price] = {}
    circuit_breaker_failures: int = Field(default=5, ge=1, le=100)
    circuit_breaker_reset_seconds: float = Field(default=30.0, gt=0, le=3600)

    @model_validator(mode="after")
    def _references_resolve(self) -> RoutingConfig:
        for provider in self.models:
            if provider not in self.providers:
                raise ValueError(f"models: unknown provider {provider!r}")
        for key in self.pricing:
            provider, _, model = key.partition("/")
            if provider not in self.providers or not model:
                raise ValueError(f"pricing key {key!r} must be '<known provider>/<model>'")
        return self

    def model_id(self, provider: str, profile: str, default: str) -> str:
        for name, model_id in self.models.get(provider, {}).items():
            if name == profile:
                return model_id
        return default

    def price_for(self, ref: ModelRef) -> Price | None:
        return self.pricing.get(f"{ref.provider}/{ref.model}")


def load_routing_config(path: Path) -> RoutingConfig:
    with path.open(encoding="utf-8") as fh:
        return RoutingConfig.model_validate(yaml.safe_load(fh))
