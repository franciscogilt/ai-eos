"""Versioned agent contract (schema `aieos/v1`).

A contract is the single source of truth for what an agent is allowed to do.
It is authored as YAML in `engineering/agent-definitions/`, validated here,
hashed, and stored in `agent_versions.contract` when published.

Runtime enforcement does not trust the contract's prose: the tool gateway reads
`tools` and `max_permission_level` from the *stored* contract on every call.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"
AGENT_KEY_PATTERN = r"^[a-z][a-z0-9_]{1,62}$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class PermissionLevel(IntEnum):
    """Four permission levels from the governance model."""

    READ_ONLY = 1
    ISOLATED_EXECUTION = 2
    CONTROLLED_SHARED = 3
    SENSITIVE = 4


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRef(_Strict):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    model: str = Field(min_length=1)


class ModelConfig(_Strict):
    """Model selection is by *profile*; the ModelRouter maps profile -> concrete model.

    `primary`/`fallbacks` are defaults that a project's routing policy may override.
    """

    profile: Literal["reasoning_high", "reasoning_standard", "fast"]
    primary: ModelRef
    fallbacks: tuple[ModelRef, ...] = ()
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=8_000, gt=0, le=128_000)


class ExecutionLimits(_Strict):
    max_turns: int = Field(gt=0, le=200)
    max_tool_calls: int = Field(ge=0, le=1_000)
    max_total_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)
    timeout_seconds: int = Field(gt=0, le=6 * 3600)


class QualityGate(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    blocking: bool = True


class FailureHandling(_Strict):
    max_retries: int = Field(ge=0, le=5)
    retry_on: tuple[Literal["model_error", "schema_violation", "tool_transient", "timeout"], ...]
    on_exhausted: Literal["escalate_to_supervisor", "request_human", "fail_workflow"]


class Independence(_Strict):
    """Separation-of-duties rules checked by the orchestrator, not by the model."""

    may_review_own_output: bool = False
    must_use_different_model_from: tuple[str, ...] = ()


class AgentContract(_Strict):
    api_version: Literal["aieos/v1"]
    key: str = Field(pattern=AGENT_KEY_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(pattern=SEMVER_PATTERN)
    purpose: str = Field(min_length=10)
    responsibilities: tuple[str, ...] = Field(min_length=1)
    prohibited: tuple[str, ...] = ()
    instructions_path: str = Field(description="Path relative to engineering/prompts/")
    input_schema: str = Field(description="Name of a model in aieos.contracts.io")
    output_schema: str = Field(description="Name of a model in aieos.contracts.io")
    model: ModelConfig
    tools: tuple[str, ...] = ()
    max_permission_level: PermissionLevel
    limits: ExecutionLimits
    quality_gates: tuple[QualityGate, ...] = ()
    failure_handling: FailureHandling
    independence: Independence = Independence()

    @field_validator("tools")
    @classmethod
    def _tools_well_formed_and_unique(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        import re

        pattern = re.compile(TOOL_NAME_PATTERN)
        bad = [t for t in tools if not pattern.match(t)]
        if bad:
            raise ValueError(f"malformed tool names: {bad}")
        if len(set(tools)) != len(tools):
            raise ValueError("duplicate tool names")
        return tools

    @field_validator("instructions_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("instructions_path must be relative and must not traverse upwards")
        return value

    @model_validator(mode="after")
    def _gate_keys_unique(self) -> AgentContract:
        keys = [g.key for g in self.quality_gates]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate quality gate keys")
        return self
