"""OpenAI Chat Completions (POST {base}/chat/completions).

Used for OpenAI itself (`kind: openai`) and for any server that speaks the same protocol
(`kind: openai_compatible`): gateways, vLLM, Ollama, LM Studio and similar.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from aieos.providers.adapters._http import HttpTransport, parse_arguments, safe_call_id
from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ChatResponse,
    ToolCall,
    ToolResultsMessage,
    UserMessage,
)
from aieos.providers.pricing import TokenUsage

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def build_body(model: str, request: ChatRequest, *, kind: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    for m in request.messages:
        if isinstance(m, UserMessage):
            messages.append({"role": "user", "content": m.text})
        elif isinstance(m, AssistantMessage):
            item: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": safe_call_id(c.id),
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                    }
                    for c in m.tool_calls
                ]
            messages.append(item)
        else:
            assert isinstance(m, ToolResultsMessage)
            for r in m.results:
                content = f"ERROR: {r.content}" if r.is_error else r.content
                messages.append({"role": "tool", "tool_call_id": safe_call_id(r.call_id), "content": content})
    body: dict[str, Any] = {"model": model, "messages": messages}
    # OpenAI's current models take max_completion_tokens; most compatible servers take max_tokens.
    body["max_completion_tokens" if kind == "openai" else "max_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.tools:
        body["tools"] = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in request.tools
        ]
        body["tool_choice"] = request.tool_choice
    return body


def parse_response(data: dict[str, Any], request_id: str | None, *, kind: str) -> ChatResponse:
    choices = data.get("choices") or [{}]
    choice = choices[0]
    msg = choice.get("message") or {}
    calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args, error = parse_arguments(fn.get("arguments"))
        calls.append(
            ToolCall(
                id=str(tc.get("id") or f"call_{i}"), name=str(fn.get("name")), arguments=args, arguments_error=error
            )
        )
    u = data.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    usage = TokenUsage(
        input_tokens=int(u.get("prompt_tokens") or 0),  # includes cached tokens
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        output_tokens=int(u.get("completion_tokens") or 0),  # includes reasoning tokens
    )
    message = AssistantMessage(text=msg.get("content") or "", tool_calls=tuple(calls), provider=kind)
    return ChatResponse(message, usage, request_id or data.get("id"), choice.get("finish_reason"))


class OpenAIChatModel:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None,
        kind: str = "openai",
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if kind == "openai_compatible" and not base_url:
            raise ValueError("openai_compatible needs a base URL")
        self._model = model
        self._kind = kind
        self._http = HttpTransport(
            provider=kind,
            base_url=base_url or DEFAULT_BASE_URL,
            headers={"authorization": f"Bearer {api_key}"} if api_key else {},  # Ollama needs no key
            timeout_seconds=timeout_seconds,
            request_id_header="x-request-id",
            transport=transport,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        data, request_id = await self._http.post("/chat/completions", build_body(self._model, request, kind=self._kind))
        return parse_response(data, request_id, kind=self._kind)
