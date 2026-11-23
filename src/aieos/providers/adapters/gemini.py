"""Google Gemini API (POST /v1beta/models/{model}:generateContent).

Gemini 3+ models attach a `thoughtSignature` to function-call parts and reject a follow-up
request that does not echo it back. Parts produced by Gemini are therefore stored verbatim in
`provider_data` and resent unchanged. Function calls produced by another provider (after a
fallback) have no signature; for those Google documents the placeholder
`skip_thought_signature_validator`, which is a last resort that may reduce quality.
"""

from __future__ import annotations

import itertools
from typing import Any
from urllib.parse import quote

import httpx

from aieos.providers.adapters._http import HttpTransport, parse_arguments
from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ChatResponse,
    Message,
    ProviderRequestError,
    ToolCall,
    ToolResultsMessage,
    UserMessage,
)
from aieos.providers.pricing import TokenUsage

KIND = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
FOREIGN_CALL_SIGNATURE = "skip_thought_signature_validator"


def _model_parts(m: AssistantMessage) -> list[dict[str, Any]]:
    if m.provider == KIND and isinstance(m.provider_data.get("parts"), list):
        return list(m.provider_data["parts"])
    parts: list[dict[str, Any]] = []
    if m.text:
        parts.append({"text": m.text})
    for i, c in enumerate(m.tool_calls):
        part: dict[str, Any] = {"functionCall": {"name": c.name, "args": c.arguments}}
        if i == 0:
            part["thoughtSignature"] = FOREIGN_CALL_SIGNATURE
        parts.append(part)
    return parts or [{"text": "(no content)"}]


def _content(m: Message) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(m, UserMessage):
        return "user", [{"text": m.text}]
    if isinstance(m, AssistantMessage):
        return "model", _model_parts(m)
    assert isinstance(m, ToolResultsMessage)
    return "user", [
        {"functionResponse": {"name": r.name, "response": {"error" if r.is_error else "content": r.content}}}
        for r in m.results
    ]


def build_body(request: ChatRequest) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    for m in request.messages:
        role, parts = _content(m)
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    generation: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
    if request.temperature is not None:
        generation["temperature"] = request.temperature
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": request.system}]},
        "contents": contents,
        "generationConfig": generation,
    }
    if request.tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parametersJsonSchema": t.parameters}
                    for t in request.tools
                ]
            }
        ]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY" if request.tool_choice == "required" else "AUTO"}}
    return body


def parse_response(data: dict[str, Any]) -> ChatResponse:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise ProviderRequestError(f"gemini: request blocked ({reason})")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    texts: list[str] = []
    calls: list[ToolCall] = []
    counter = itertools.count()
    response_id = data.get("responseId") or "gemini"
    for part in parts:
        if "functionCall" in part:
            fc = part["functionCall"]
            args, error = parse_arguments(fc.get("args"))
            call_id = str(fc.get("id") or f"{response_id}_{next(counter)}")
            calls.append(ToolCall(id=call_id, name=str(fc.get("name")), arguments=args, arguments_error=error))
        elif "text" in part and not part.get("thought"):
            texts.append(part["text"])
    u = data.get("usageMetadata") or {}
    usage = TokenUsage(
        input_tokens=int(u.get("promptTokenCount") or 0) + int(u.get("toolUsePromptTokenCount") or 0),
        cached_input_tokens=int(u.get("cachedContentTokenCount") or 0),
        # Thinking tokens are billed as output.
        output_tokens=int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0),
    )
    message = AssistantMessage(
        text="".join(texts), tool_calls=tuple(calls), provider=KIND, provider_data={"parts": parts}
    )
    return ChatResponse(message, usage, data.get("responseId"), candidate.get("finishReason"))


class GeminiChatModel:
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
            headers={"x-goog-api-key": api_key},
            timeout_seconds=timeout_seconds,
            request_id_header=None,
            transport=transport,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        data, _ = await self._http.post(
            f"/models/{quote(self._model, safe='-._')}:generateContent", build_body(request)
        )
        return parse_response(data)
