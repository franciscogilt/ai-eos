"""Anthropic Messages API (POST /v1/messages)."""

from __future__ import annotations

from typing import Any

import httpx

from aieos.providers.adapters._http import HttpTransport, parse_arguments, safe_call_id
from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolResultsMessage,
    UserMessage,
)
from aieos.providers.pricing import TokenUsage

KIND = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


def _assistant_blocks(m: AssistantMessage) -> list[dict[str, Any]]:
    if m.provider == KIND and isinstance(m.provider_data.get("content"), list):
        return list(m.provider_data["content"])  # echo verbatim (keeps any thinking blocks valid)
    blocks: list[dict[str, Any]] = []
    if m.text:
        blocks.append({"type": "text", "text": m.text})
    for c in m.tool_calls:
        blocks.append({"type": "tool_use", "id": safe_call_id(c.id), "name": c.name, "input": c.arguments})
    return blocks or [{"type": "text", "text": "(no content)"}]


def _turn(m: Message) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(m, UserMessage):
        return "user", [{"type": "text", "text": m.text}]
    if isinstance(m, AssistantMessage):
        return "assistant", _assistant_blocks(m)
    assert isinstance(m, ToolResultsMessage)
    return "user", [
        {"type": "tool_result", "tool_use_id": safe_call_id(r.call_id), "content": r.content, "is_error": r.is_error}
        for r in m.results
    ]


def build_body(model: str, request: ChatRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for m in request.messages:
        role, content = _turn(m)
        if messages and messages[-1]["role"] == role:  # roles must alternate: merge
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_output_tokens,
        # Tools and system prompt are identical on every turn of a run: cache them.
        "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.tools:
        body["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools
        ]
        body["tool_choice"] = {"type": "any" if request.tool_choice == "required" else "auto"}
    return body


def parse_response(data: dict[str, Any], request_id: str | None) -> ChatResponse:
    content = data.get("content") or []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            args, error = parse_arguments(block.get("input"))
            calls.append(
                ToolCall(id=str(block.get("id")), name=str(block.get("name")), arguments=args, arguments_error=error)
            )
    u = data.get("usage") or {}
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    usage = TokenUsage(
        # Anthropic's input_tokens excludes cache reads and writes; ours includes them.
        input_tokens=int(u.get("input_tokens") or 0) + cache_read + cache_write,
        cached_input_tokens=cache_read,
        output_tokens=int(u.get("output_tokens") or 0),
        cache_write_input_tokens=cache_write,
    )
    message = AssistantMessage(
        text="".join(texts), tool_calls=tuple(calls), provider=KIND, provider_data={"content": content}
    )
    return ChatResponse(message, usage, request_id or data.get("id"), data.get("stop_reason"))


class AnthropicChatModel:
    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._http = HttpTransport(
            provider=KIND,
            base_url=base_url or DEFAULT_BASE_URL,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            timeout_seconds=timeout_seconds,
            request_id_header="request-id",
            transport=transport,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        data, request_id = await self._http.post("/v1/messages", build_body(self._model, request))
        return parse_response(data, request_id)
