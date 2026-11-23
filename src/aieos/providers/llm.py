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


# ------------------------------------------------------------------ checkpoint serialization
def message_to_dict(m: Message) -> dict[str, Any]:
    if isinstance(m, UserMessage):
        return {"role": "user", "text": m.text}
    if isinstance(m, AssistantMessage):
        return {
            "role": "assistant",
            "text": m.text,
            "provider": m.provider,
            "provider_data": m.provider_data,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments, "arguments_error": c.arguments_error}
                for c in m.tool_calls
            ],
        }
    return {
        "role": "tool",
        "results": [
            {"call_id": r.call_id, "name": r.name, "content": r.content, "is_error": r.is_error} for r in m.results
        ],
    }


def message_from_dict(d: dict[str, Any]) -> Message:
    role = d.get("role")
    if role == "user":
        return UserMessage(str(d["text"]))
    if role == "assistant":
        calls = tuple(
            ToolCall(str(c["id"]), str(c["name"]), dict(c.get("arguments") or {}), c.get("arguments_error"))
            for c in d.get("tool_calls", [])
        )
        return AssistantMessage(str(d.get("text", "")), calls, d.get("provider"), dict(d.get("provider_data") or {}))
    if role == "tool":
        return ToolResultsMessage(
            tuple(
                ToolResultItem(str(r["call_id"]), str(r["name"]), str(r["content"]), bool(r.get("is_error")))
                for r in d.get("results", [])
            )
        )
    raise ValueError(f"unknown message role {role!r}")
