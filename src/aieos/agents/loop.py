"""The agent loop: model turn -> tool calls through the gateway -> results -> next turn.

The loop ends only when the model calls `submit_output` with arguments that validate against
the step's output model. Every turn must call a tool (tool_choice "required"), so free text
never counts as an answer and output parsing never depends on a vendor's JSON mode.
An invalid submission is returned to the model as a tool error inside the same run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from aieos.agents.errors import AgentLimitError
from aieos.core.logging import get_logger
from aieos.providers.llm import (
    ChatModel,
    ChatRequest,
    Message,
    ToolResultItem,
    ToolResultsMessage,
    ToolSpec,
    UserMessage,
)
from aieos.providers.schema import inline_refs

log = get_logger(__name__)

SUBMIT_TOOL = "submit_output"

ToolFn = Callable[[dict[str, Any]], Awaitable[tuple[str, bool]]]  # -> (content, is_error)


@dataclass(frozen=True)
class AgentTool:
    spec: ToolSpec
    invoke: ToolFn


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    system: str
    output_type: type[BaseModel]
    tools: dict[str, AgentTool] = field(default_factory=dict)
    temperature: float | None = None
    max_output_tokens: int = 8_000


def submit_spec(output_type: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_TOOL,
        description=(
            "Submit your final answer and end the task. Call it once, when you are done; "
            "its arguments are the answer and must match this schema."
        ),
        parameters=inline_refs(output_type.model_json_schema()),
    )


def _validation_summary(exc: ValidationError) -> str:
    lines = [f"- {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()[:20]]
    return "\n".join(lines)


async def run_agent(agent: AgentDefinition, message: str, *, model: ChatModel, max_turns: int) -> BaseModel:
    if SUBMIT_TOOL in agent.tools:
        raise ValueError(f"{SUBMIT_TOOL} is reserved")
    specs = (*(t.spec for t in agent.tools.values()), submit_spec(agent.output_type))
    messages: list[Message] = [UserMessage(message)]
    for turn in range(1, max_turns + 1):
        response = await model.complete(
            ChatRequest(
                system=agent.system,
                messages=tuple(messages),
                tools=specs,
                tool_choice="required",
                temperature=agent.temperature,
                max_output_tokens=agent.max_output_tokens,
            )
        )
        reply = response.message
        messages.append(reply)
        if not reply.tool_calls:
            hint = (
                " Your reply was cut off by the output limit; be more concise."
                if response.stop_reason in ("max_tokens", "length", "MAX_TOKENS")
                else ""
            )
            messages.append(UserMessage(f"Respond by calling a tool. When finished, call {SUBMIT_TOOL}.{hint}"))
            continue

        results: list[ToolResultItem] = []
        final: BaseModel | None = None
        for call in reply.tool_calls:
            if call.arguments_error:
                results.append(ToolResultItem(call.id, call.name, f"Rejected: {call.arguments_error}.", True))
            elif call.name == SUBMIT_TOOL:
                if final is not None:
                    results.append(ToolResultItem(call.id, call.name, "Ignored: an answer was already accepted.", True))
                    continue
                try:
                    final = agent.output_type.model_validate(call.arguments)
                except ValidationError as exc:
                    log.warning("agent.schema_violation", agent=agent.name, turn=turn, errors=exc.error_count())
                    results.append(
                        ToolResultItem(
                            call.id,
                            call.name,
                            "Answer rejected: it does not match the schema. Fix these and call "
                            f"{SUBMIT_TOOL} again:\n{_validation_summary(exc)}",
                            True,
                        )
                    )
                else:
                    results.append(ToolResultItem(call.id, call.name, "Accepted."))
            elif (tool := agent.tools.get(call.name)) is not None:
                content, is_error = await tool.invoke(call.arguments)
                results.append(ToolResultItem(call.id, call.name, content, is_error))
            else:
                results.append(ToolResultItem(call.id, call.name, f"Unknown tool {call.name!r}.", True))
        if final is not None:
            return final
        messages.append(ToolResultsMessage(tuple(results)))
    raise AgentLimitError(f"{agent.name} exceeded {max_turns} turns")
