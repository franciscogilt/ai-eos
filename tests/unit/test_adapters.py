"""Vendor adapters against recorded-shape HTTP exchanges (httpx.MockTransport; no network).

These pin our translation of the neutral conversation to each vendor's documented wire
format. They cannot prove a live API still accepts it: that is what `make eval` with real
keys is for.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from aieos.providers.adapters.anthropic import AnthropicChatModel
from aieos.providers.adapters.gemini import FOREIGN_CALL_SIGNATURE, GeminiChatModel
from aieos.providers.adapters.openai_chat import OpenAIChatModel
from aieos.providers.llm import (
    AssistantMessage,
    ChatRequest,
    ProviderRequestError,
    ProviderTransientError,
    ToolCall,
    ToolResultItem,
    ToolResultsMessage,
    ToolSpec,
    UserMessage,
)
from aieos.providers.pricing import TokenUsage
from aieos.providers.schema import inline_refs

TOOL = ToolSpec("repo__read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
CALL = ToolCall(id="toolu_01", name="repo__read_file", arguments={"path": "README.md"})


def conversation(assistant: AssistantMessage) -> ChatRequest:
    return ChatRequest(
        system="You are careful.",
        messages=(
            UserMessage("Read the readme"),
            assistant,
            ToolResultsMessage((ToolResultItem("toolu_01", "repo__read_file", "# Title", False),)),
            UserMessage("Now answer."),
        ),
        tools=(TOOL,),
        tool_choice="required",
        temperature=0.2,
        max_output_tokens=1000,
    )


class Recorder:
    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def transport(rec: Recorder) -> httpx.MockTransport:
    return httpx.MockTransport(rec)


# ------------------------------------------------------------------ Anthropic
ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [
        {"type": "text", "text": "Reading."},
        {"type": "tool_use", "id": "toolu_02", "name": "repo__read_file", "input": {"path": "a.py"}},
    ],
    "stop_reason": "tool_use",
    "usage": {
        "input_tokens": 100,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 850,
        "output_tokens": 40,
    },
}


def test_anthropic_request_and_response():
    rec = Recorder(body=ANTHROPIC_REPLY, headers={"request-id": "req_abc"})
    model = AnthropicChatModel("claude-x", api_key="test-key", transport=transport(rec))
    out = asyncio.run(model.complete(conversation(AssistantMessage("", (CALL,)))))

    req = rec.requests[0]
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "test-key"
    assert req.headers["anthropic-version"] == "2023-06-01"
    body = rec.sent
    assert body["model"] == "claude-x" and body["max_tokens"] == 1000 and body["temperature"] == 0.2
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "any"}
    assert body["tools"][0]["input_schema"] == TOOL.parameters
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]  # tool results and the follow-up text merged
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "toolu_01", "name": "repo__read_file", "input": {"path": "README.md"}}
    ]
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_01",
        "content": "# Title",
        "is_error": False,
    }

    assert out.message.text == "Reading." and out.message.tool_calls[0].arguments == {"path": "a.py"}
    assert out.usage == TokenUsage(1000, 850, 40, 50)  # input includes cache reads and writes
    assert out.request_id == "req_abc" and out.stop_reason == "tool_use"


def test_anthropic_echoes_its_own_content_verbatim():
    rec = Recorder(body=ANTHROPIC_REPLY)
    model = AnthropicChatModel("claude-x", api_key="k", transport=transport(rec))
    first = asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))
    asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"), first.message, UserMessage("go")))))
    assert rec.sent["messages"][1]["content"] == ANTHROPIC_REPLY["content"]
    assert "temperature" not in rec.sent and "tools" not in rec.sent


# ------------------------------------------------------------------ OpenAI / compatible
OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "repo__read_file", "arguments": '{"path":"b.py"}'},
                    },
                    {
                        "id": "call_10",
                        "type": "function",
                        "function": {"name": "repo__read_file", "arguments": "{oops"},
                    },
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 900, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 512}},
}


def test_openai_request_and_response():
    rec = Recorder(body=OPENAI_REPLY, headers={"x-request-id": "req_o"})
    model = OpenAIChatModel("gpt-x", api_key="test-key", transport=transport(rec))
    out = asyncio.run(model.complete(conversation(AssistantMessage("thinking", (CALL,)))))

    req = rec.requests[0]
    assert req.url.path == "/v1/chat/completions" and req.headers["authorization"] == "Bearer test-key"
    body = rec.sent
    assert body["max_completion_tokens"] == 1000 and "max_tokens" not in body
    assert body["tool_choice"] == "required"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool", "user"]
    assistant = body["messages"][2]
    assert assistant["content"] == "thinking"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "README.md"}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "toolu_01", "content": "# Title"}

    good, bad = out.message.tool_calls
    assert good.arguments == {"path": "b.py"} and good.arguments_error is None
    assert bad.arguments == {} and bad.arguments_error == "arguments are not valid JSON"
    assert out.usage == TokenUsage(900, 512, 30) and out.request_id == "req_o"


def test_openai_compatible_uses_base_url_and_max_tokens():
    rec = Recorder(body=OPENAI_REPLY)
    model = OpenAIChatModel(
        "llama", api_key="k", kind="openai_compatible", base_url="http://gw.test/v1/", transport=transport(rec)
    )
    asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),), max_output_tokens=50)))
    assert str(rec.requests[0].url) == "http://gw.test/v1/chat/completions"
    assert rec.sent["max_tokens"] == 50 and "max_completion_tokens" not in rec.sent
    with pytest.raises(ValueError):
        OpenAIChatModel("m", api_key="k", kind="openai_compatible")


def test_tool_error_results_are_marked_for_openai():
    rec = Recorder(body=OPENAI_REPLY)
    model = OpenAIChatModel("gpt-x", api_key="k", transport=transport(rec))
    req = ChatRequest(
        system="s",
        messages=(
            UserMessage("hi"),
            AssistantMessage("", (CALL,)),
            ToolResultsMessage((ToolResultItem("toolu_01", "repo__read_file", "denied", True),)),
        ),
    )
    asyncio.run(model.complete(req))
    assert rec.sent["messages"][-1]["content"] == "ERROR: denied"


# ------------------------------------------------------------------ Gemini
GEMINI_PARTS = [
    {"functionCall": {"name": "repo__read_file", "args": {"path": "c.py"}}, "thoughtSignature": "SIG_A"},
    {"functionCall": {"name": "repo__read_file", "args": {"path": "d.py"}}},
]
GEMINI_REPLY = {
    "responseId": "resp_g",
    "candidates": [{"content": {"role": "model", "parts": GEMINI_PARTS}, "finishReason": "STOP"}],
    "usageMetadata": {
        "promptTokenCount": 1200,
        "cachedContentTokenCount": 1000,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 80,
    },
}


def test_gemini_request_and_response():
    rec = Recorder(body=GEMINI_REPLY)
    model = GeminiChatModel("gemini-x", api_key="test-key", transport=transport(rec))
    out = asyncio.run(model.complete(conversation(AssistantMessage("", (CALL,), provider="anthropic"))))

    req = rec.requests[0]
    assert req.url.path == "/v1beta/models/gemini-x:generateContent"
    assert req.headers["x-goog-api-key"] == "test-key" and "key=" not in str(req.url)
    body = rec.sent
    assert body["systemInstruction"] == {"parts": [{"text": "You are careful."}]}
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == TOOL.parameters
    assert body["generationConfig"] == {"maxOutputTokens": 1000, "temperature": 0.2}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    # A call made by another provider has no signature: Google's documented placeholder is sent.
    assert body["contents"][1]["parts"][0]["thoughtSignature"] == FOREIGN_CALL_SIGNATURE
    assert body["contents"][2]["parts"][0] == {
        "functionResponse": {"name": "repo__read_file", "response": {"content": "# Title"}}
    }

    assert [c.arguments["path"] for c in out.message.tool_calls] == ["c.py", "d.py"]
    assert len({c.id for c in out.message.tool_calls}) == 2
    assert out.usage == TokenUsage(1200, 1000, 100)  # thinking tokens billed as output
    assert out.request_id == "resp_g"


def test_gemini_echoes_its_own_parts_with_signatures():
    rec = Recorder(body=GEMINI_REPLY)
    model = GeminiChatModel("gemini-x", api_key="k", transport=transport(rec))
    first = asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))
    results = ToolResultsMessage(
        tuple(ToolResultItem(c.id, c.name, "x", i == 1) for i, c in enumerate(first.message.tool_calls))
    )
    asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"), first.message, results))))
    contents = rec.sent["contents"]
    assert contents[1]["parts"] == GEMINI_PARTS  # SIG_A preserved exactly, no placeholder added
    assert contents[2]["parts"][1]["functionResponse"]["response"] == {"error": "x"}


def test_gemini_block_is_a_request_error():
    rec = Recorder(body={"promptFeedback": {"blockReason": "SAFETY"}})
    model = GeminiChatModel("g", api_key="k", transport=transport(rec))
    with pytest.raises(ProviderRequestError, match="SAFETY"):
        asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))


# ------------------------------------------------------------------ shared HTTP error policy
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (429, {"error": "rate"}, ProviderTransientError),
        (503, {"error": "down"}, ProviderTransientError),
        (529, {"error": "overloaded"}, ProviderTransientError),
        (200, "<html>proxy page</html>", ProviderTransientError),
        (400, {"error": {"message": "unknown model"}}, ProviderRequestError),
        (401, {"error": "bad key"}, ProviderRequestError),
    ],
)
def test_http_error_classification(status, body, error):
    rec = Recorder(status=status, body=body)
    model = AnthropicChatModel("m", api_key="k", transport=transport(rec))
    with pytest.raises(error):
        asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_network_failures_are_transient(exc):
    model = GeminiChatModel("m", api_key="k", transport=transport(Recorder(body=exc)))
    with pytest.raises(ProviderTransientError):
        asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))


def test_request_error_never_echoes_the_api_key():
    rec = Recorder(status=400, body={"error": "bad"})
    model = OpenAIChatModel("m", api_key="sk-test-secret", transport=transport(rec))
    with pytest.raises(ProviderRequestError) as err:
        asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))
    assert "sk-test-secret" not in str(err.value)


def test_foreign_call_ids_are_made_safe_for_every_vendor():
    rec = Recorder(body=ANTHROPIC_REPLY)
    model = AnthropicChatModel("m", api_key="k", transport=transport(rec))
    odd = ToolCall(id="resp/1:0", name="repo__read_file", arguments={})
    req = ChatRequest(
        system="s",
        messages=(
            UserMessage("hi"),
            AssistantMessage("", (odd,), provider="gemini"),
            ToolResultsMessage((ToolResultItem("resp/1:0", "repo__read_file", "ok"),)),
        ),
    )
    asyncio.run(model.complete(req))
    msgs = rec.sent["messages"]
    assert msgs[1]["content"][0]["id"] == msgs[2]["content"][0]["tool_use_id"] == "resp_1_0"


# ------------------------------------------------------------------ schema inlining
def test_inline_refs():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A"}, "b": {"type": "array", "items": {"$ref": "#/$defs/A"}}},
        "$defs": {"A": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    out = inline_refs(schema)
    assert "$defs" not in out and "$ref" not in json.dumps(out)
    assert out["properties"]["b"]["items"]["properties"]["x"] == {"type": "string"}
    assert "$defs" in schema  # input untouched


def test_inline_refs_refuses_recursion():
    schema = {"$ref": "#/$defs/N", "$defs": {"N": {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}}}
    with pytest.raises(ValueError, match="recursive"):
        inline_refs(schema)
