"""Console CLIs as models (Claude Code, Gemini CLI, Codex) and keyless local servers (Ollama).

The real CLIs need a signed-in account, so these tests use stand-ins that follow each CLI's
documented headless output. One test runs a real subprocess (a fake `claude` script) to check the
working directory, environment and stdin handling end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from aieos.agents.loop import AgentDefinition, run_agent
from aieos.providers.adapters.cli import CliChatModel, Completed, cli_env, run_process
from aieos.providers.adapters.openai_chat import OpenAIChatModel
from aieos.providers.adapters.text_protocol import parse_reply, render_conversation, render_system
from aieos.providers.config import ProviderConfig, load_routing_config
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
from aieos.providers.router import ModelNotConfiguredError, ModelRouter, http_model_factory

TOOL = ToolSpec("repo__read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
REQ = ChatRequest(
    system="You are the Developer.",
    messages=(
        UserMessage("Implement T-1"),
        AssistantMessage("", (ToolCall("t1", "repo__read_file", {"path": "a.py"}),)),
        ToolResultsMessage((ToolResultItem("t1", "repo__read_file", "print(1)"),)),
    ),
    tools=(TOOL,),
    tool_choice="required",
)
REPLY = json.dumps({"tool_calls": [{"name": "repo__read_file", "arguments": {"path": "b.py"}}]})


# ------------------------------------------------------------------ text protocol
def test_protocol_renders_tools_and_history():
    system = render_system(REQ)
    assert "You are the Developer." in system and '"repo__read_file"' in system and "at least one tool" in system
    convo = render_conversation(REQ)
    assert convo.index("Implement T-1") < convo.index('"path": "a.py"') < convo.index("print(1)")
    assert convo.endswith("one JSON object, nothing else)")


@pytest.mark.parametrize(
    "text",
    [REPLY, f"Sure!\n```json\n{REPLY}\n```\n", f"Here you go: {REPLY} thanks"],
)
def test_reply_parsing_is_tolerant(text):
    msg = parse_reply(text, provider="cli:x")
    assert [c.arguments for c in msg.tool_calls] == [{"path": "b.py"}] and msg.text == ""


def test_unparseable_reply_is_plain_text_and_bad_arguments_are_flagged():
    assert parse_reply("I think we should...", provider="cli:x").tool_calls == ()
    bad = parse_reply('{"tool_calls": [{"name": "x", "arguments": "oops"}]}', provider="cli:x")
    assert bad.tool_calls[0].arguments_error


# ------------------------------------------------------------------ CLI adapters with a scripted runner
class Script:
    def __init__(self, stdout: str = "", *, exit_code: int = 0, stderr: str = "", last_message: str | None = None):
        self.stdout, self.exit_code, self.stderr, self.last_message = stdout, exit_code, stderr, last_message
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv, stdin, cwd, env, timeout):
        self.calls.append({"argv": argv, "stdin": stdin.decode(), "cwd": cwd, "env": env, "listing": os.listdir(cwd)})
        if self.last_message is not None:
            Path(argv[argv.index("-o") + 1]).write_text(self.last_message)
        return Completed(self.exit_code, self.stdout.encode(), self.stderr.encode())


def complete(model: CliChatModel):
    return asyncio.run(model.complete(REQ))


def test_claude_code_headless():
    out = json.dumps(
        {
            "type": "result",
            "result": REPLY,
            "is_error": False,
            "session_id": "s-1",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 90,
                "cache_creation_input_tokens": 0,
                "output_tokens": 7,
            },
        }
    )
    script = Script(out)
    resp = complete(CliChatModel("claude_code", "default", runner=script))
    (call,) = script.calls
    argv = call["argv"]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"] and "--model" not in argv
    assert argv[argv.index("--tools") + 1] == ""  # the CLI's own tools are off
    assert "You are the Developer." in argv[argv.index("--system-prompt") + 1]
    assert "Implement T-1" in call["stdin"] and call["listing"] == []  # empty working directory
    assert resp.message.tool_calls[0].name == "repo__read_file"
    assert (resp.usage.input_tokens, resp.usage.cached_input_tokens, resp.usage.output_tokens) == (100, 90, 7)

    script2 = Script(out)
    complete(CliChatModel("claude_code", "sonnet", runner=script2, extra_args=("--max-turns", "1")))
    assert script2.calls[0]["argv"][-4:] == ["--model", "sonnet", "--max-turns", "1"]


def test_gemini_cli_headless():
    out = json.dumps(
        {
            "response": REPLY,
            "stats": {"models": {"g": {"tokens": {"prompt": 50, "candidates": 5, "thoughts": 3, "cached": 20}}}},
        }
    )
    script = Script(out)
    resp = complete(CliChatModel("gemini_cli", "gemini-x", runner=script))
    argv, stdin = script.calls[0]["argv"], script.calls[0]["stdin"]
    assert argv[0] == "gemini" and argv[argv.index("-m") + 1] == "gemini-x" and "--output-format" in argv
    assert stdin.startswith("You are the Developer.")  # instructions lead the stdin text
    assert (resp.usage.input_tokens, resp.usage.cached_input_tokens, resp.usage.output_tokens) == (50, 20, 8)


def test_codex_headless_read_only():
    events = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 40,
                        "cached_input_tokens": 10,
                        "output_tokens": 6,
                        "reasoning_output_tokens": 4,
                    },
                }
            ),
        ]
    )
    script = Script(events, last_message=REPLY)
    resp = complete(CliChatModel("codex_cli", "default", runner=script))
    argv = script.calls[0]["argv"]
    assert argv[:2] == ["codex", "exec"] and argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[-1] == "-" and "--ephemeral" in argv
    assert resp.message.tool_calls and resp.usage.output_tokens == 10


def test_missing_usage_is_estimated_so_budgets_still_apply():
    resp = complete(CliChatModel("gemini_cli", "default", runner=Script(json.dumps({"response": REPLY}))))
    assert resp.usage.input_tokens > 0 and resp.usage.output_tokens > 0


@pytest.mark.parametrize(
    ("script", "error"),
    [
        (Script(exit_code=1, stderr="Error: 429 rate limit exceeded"), ProviderTransientError),
        (Script(exit_code=1, stderr="Please run /login"), ProviderRequestError),
        (Script("not json"), ProviderTransientError),
        (Script(json.dumps({"result": "Credit balance too low", "is_error": True})), ProviderRequestError),
    ],
)
def test_cli_failures_are_classified(script, error):
    with pytest.raises(error):
        complete(CliChatModel("claude_code", "default", runner=script))


def test_environment_is_reduced():
    env = cli_env(
        {
            "HOME": "/home/u",
            "PATH": "/bin",
            "DATABASE_URL": "postgresql://secret",
            "GITHUB_APP_PRIVATE_KEY_PATH": "/k",
            "ANTHROPIC_API_KEY": "",
            "XDG_CONFIG_HOME": "/c",
            "AWS_SECRET_ACCESS_KEY": "x",
        }
    )
    assert set(env) == {"HOME", "PATH", "ANTHROPIC_API_KEY", "XDG_CONFIG_HOME"}


def test_real_subprocess_with_a_stand_in_cli(tmp_path, monkeypatch):
    """Runs a fake `claude` executable through the real process runner."""
    fake = tmp_path / "claude"
    body = """
import json, os, sys
prompt = sys.stdin.read()
seen = "|".join([os.getcwd(), str(len(os.listdir("."))), str("DATABASE_URL" in os.environ)])
reply = {"tool_calls": [{"name": "submit_output", "arguments": {"answer": seen}}]}
usage = {"input_tokens": len(prompt) // 4, "output_tokens": 5}
print(json.dumps({"result": json.dumps(reply), "is_error": False, "usage": usage}))
"""
    fake.write_text(f"#!{sys.executable}\n{body}")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")

    class Answer(BaseModel):
        answer: str

    model = CliChatModel("claude_code", "default", command=str(fake), runner=run_process, timeout_seconds=30)
    agent = AgentDefinition(name="t", system="s", output_type=Answer)
    result = asyncio.run(run_agent(agent, "go", model=model, max_turns=2))
    cwd, entries, leaked = result.answer.split("|")
    assert "aieos-cli-" in cwd and entries == "0" and leaked == "False"
    assert not Path(cwd).exists()  # removed after the call


def test_timeout_is_transient(tmp_path):
    slow = tmp_path / "claude"
    slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(ProviderTransientError, match="timed out"):
        complete(CliChatModel("claude_code", "default", command=str(slow), runner=run_process, timeout_seconds=0.5))


# ------------------------------------------------------------------ routing and keyless servers
ENGINEERING = Path(__file__).resolve().parents[2] / "engineering"


def test_local_routing_file_routes_every_agent_for_free(definitions_dir, prompts_dir, catalog):
    from aieos.agents.independence import check_model_independence
    from aieos.agents.registry import load_all

    cfg = load_routing_config(ENGINEERING / "model-routing.local.yaml")
    router = ModelRouter(cfg)
    contracts = {k: v.contract for k, v in load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog).items()}
    for contract in contracts.values():
        for ref in router.resolve(contract.model):
            assert cfg.price_for(ref).output_per_mtok == 0
    check_model_independence(contracts, router)  # QA still reviews with a different model family
    assert router.resolve(contracts["qa_security"].model)[0].provider == "gemini"


def test_cli_factory_needs_the_binary(monkeypatch):
    cfg = ProviderConfig(kind="cli", cli="claude_code")
    monkeypatch.setattr("shutil.which", lambda _c: None)
    with pytest.raises(ModelNotConfiguredError, match="not installed"):
        http_model_factory("anthropic", cfg, "default")
    monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}")
    assert isinstance(http_model_factory("anthropic", cfg, "default"), CliChatModel)


@pytest.mark.parametrize(
    "bad",
    [{"kind": "cli"}, {"kind": "anthropic"}, {"kind": "openai_compatible", "cli": "codex_cli", "base_url_env": "U"}],
)
def test_provider_config_rules(bad):
    with pytest.raises(ValueError):
        ProviderConfig.model_validate(bad)


def test_ollama_needs_no_key(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    cfg = ProviderConfig(kind="openai_compatible", base_url_env="OLLAMA_BASE_URL")
    model = http_model_factory("ollama", cfg, "qwen2.5-coder:14b")
    assert isinstance(model, OpenAIChatModel)
    model._http._client._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    asyncio.run(model.complete(ChatRequest(system="s", messages=(UserMessage("hi"),))))
    assert "authorization" not in seen[0].headers and seen[0].url.host == "localhost"
