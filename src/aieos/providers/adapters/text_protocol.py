"""Tool calling over plain text, for models reached through a coding CLI.

A CLI such as `claude -p` returns text, not structured tool calls, so the conversation is rendered
as text with the tool definitions and a strict reply format: one JSON object listing the tool
calls. The reply is parsed back into neutral `ToolCall`s. Anything unparseable becomes a plain
text reply, which the agent loop answers with a reminder to call a tool.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ToolCall,
    ToolResultsMessage,
    UserMessage,
)

PROTOCOL = """# Reply format (mandatory)
You are called through a program, not a chat window. You cannot run anything yourself; you act only
by requesting the tools listed below. Reply with exactly one JSON object and nothing else:

{"tool_calls": [{"name": "<tool name>", "arguments": {<arguments matching the tool's schema>}}]}

Request one or more tools per reply. Results come back in the next message. When you are finished,
call `submit_output` with your answer as its arguments.{required}"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def render_system(request: ChatRequest) -> str:
    required = "\nEvery reply must request at least one tool." if request.tool_choice == "required" else ""
    tools = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in request.tools]
    return f"{request.system}\n\n{PROTOCOL.replace('{required}', required)}\n\n# Tools\n" + json.dumps(
        tools, ensure_ascii=False, indent=1
    )


def render_conversation(request: ChatRequest) -> str:
    parts: list[str] = []
    for m in request.messages:
        if isinstance(m, UserMessage):
            parts.append(f"## USER\n{m.text}")
        elif isinstance(m, AssistantMessage):
            calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls]
            body = json.dumps({"tool_calls": calls}, ensure_ascii=False) if calls else m.text
            parts.append(f"## YOU (earlier reply)\n{body}")
        elif isinstance(m, ToolResultsMessage):
            lines = [f"[{r.call_id}] {r.name}{' (ERROR)' if r.is_error else ''}:\n{r.content}" for r in m.results]
            parts.append("## TOOL RESULTS\n" + "\n\n".join(lines))
    parts.append("## YOUR REPLY (one JSON object, nothing else)")
    return "\n\n".join(parts)


def _json_object(text: str) -> Any:
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            return json.loads(candidate[start : end + 1])
        except ValueError:
            continue
    return None


def parse_reply(text: str, *, provider: str) -> AssistantMessage:
    data = _json_object(text)
    calls: list[ToolCall] = []
    if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
        for raw in data["tool_calls"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            args = raw.get("arguments", {})
            error = None if isinstance(args, dict) else "arguments must be a JSON object"
            calls.append(
                ToolCall(
                    id=f"t{uuid.uuid4().hex[:12]}",
                    name=raw["name"],
                    arguments=args if isinstance(args, dict) else {},
                    arguments_error=error,
                )
            )
    return AssistantMessage(text="" if calls else text[:20_000], tool_calls=tuple(calls), provider=provider)


def estimate_tokens(text: str) -> int:
    """Rough count (4 characters per token) for CLIs that report no usage."""
    return max(1, len(text) // 4)
