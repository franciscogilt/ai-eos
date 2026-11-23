"""Shared HTTP handling: one place decides which failures are transient."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from aieos.providers.llm import ProviderRequestError, ProviderTransientError

# 408 timeout, 409 conflict/lock, 425 too early, 429 rate limit; 5xx (incl. 529 overloaded) below.
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429})
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def safe_call_id(call_id: str) -> str:
    """Tool-call ids cross providers on fallback; keep them inside every vendor's allowed alphabet."""
    return _SAFE_ID.sub("_", call_id)[:64] or "call"


def parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Arguments arrive as an object (Anthropic, Gemini) or a JSON string (OpenAI)."""
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}, "arguments are not valid JSON"
    if not isinstance(value, dict):
        return {}, "arguments must be a JSON object"
    return value, None


class HttpTransport:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        headers: dict[str, str],
        timeout_seconds: float,
        request_id_header: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._provider = provider
        self._request_id_header = request_id_header
        # Retries are ours (fallback + circuit breaker) and Temporal's, not the client's.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"content-type": "application/json", **headers},
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            transport=transport,
        )

    async def post(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        try:
            response = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"{self._provider}: timeout ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"{self._provider}: connection failed ({type(exc).__name__})") from None
        status = response.status_code
        if status in _TRANSIENT_STATUS or status >= 500:
            raise ProviderTransientError(f"{self._provider}: HTTP {status}")
        if status >= 400:
            # The vendor's error message explains configuration mistakes (bad model id, schema).
            raise ProviderRequestError(f"{self._provider}: HTTP {status}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError:
            raise ProviderTransientError(f"{self._provider}: response was not JSON") from None
        if not isinstance(data, dict):
            raise ProviderTransientError(f"{self._provider}: unexpected response shape")
        request_id = response.headers.get(self._request_id_header) if self._request_id_header else None
        return data, request_id

    async def aclose(self) -> None:
        await self._client.aclose()
