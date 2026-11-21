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
    # openai: OpenAI Responses API. openai_compatible: Chat Completions at base_url (any vendor or gateway).
    kind: Literal["openai", "openai_compatible"]
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    base_url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)

    @model_validator(mode="after")
    def _compatible_needs_url(self) -> ProviderConfig:
        if self.kind == "openai_compatible" and not self.base_url_env:
            raise ValueError("openai_compatible providers need base_url_env")
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
