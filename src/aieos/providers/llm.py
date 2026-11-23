"""Provider-neutral chat types: the only model interface the rest of AI-EOS sees.

A conversation is a list of `Message`s. Adapters translate it to one vendor's wire format and
back; nothing outside `aieos.providers.adapters` knows any vendor's format. Because history is
neutral, a run can fall back from one provider to another in the middle of a tool loop.

`provider_data` carries opaque per-vendor state that must be echoed back verbatim to the same
vendor (for example Gemini's thought signatures). Other vendors ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from aieos.providers.pricing import TokenUsage


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderTransientError(ProviderError):
    """Timeouts, connection failures, rate limits, 5xx. The next candidate may succeed."""


class ProviderRequestError(ProviderError):
    """The request was rejected (4xx other than rate limits): a bug or a configuration error."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str  # wire name: [A-Za-z0-9_-], at most 64 characters
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments object, without $ref


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model sent arguments that are not a JSON object; the loop reports it back.
    arguments_error: str | None = None


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str
    role: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    provider: str | None = None  # adapter kind that produced it
    provider_data: dict[str, Any] = field(default_factory=dict)
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultItem:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultsMessage:
    """Results for every tool call of the preceding assistant message, in the same order."""

    results: tuple[ToolResultItem, ...]
    role: Literal["tool"] = "tool"


Message = UserMessage | AssistantMessage | ToolResultsMessage


@dataclass(frozen=True, slots=True)
class ChatRequest:
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    # "required": the model must call a tool (our loop ends only through submit_output).
    tool_choice: Literal["auto", "required"] = "auto"
    temperature: float | None = None
    max_output_tokens: int = 8_000


@dataclass(frozen=True, slots=True)
class ChatResponse:
    message: AssistantMessage
    usage: TokenUsage
    request_id: str | None
    stop_reason: str | None = None


class ChatModel(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse: ...
