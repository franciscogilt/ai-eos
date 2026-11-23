"""Coding CLIs already signed in on this machine, used as plain text models (no API key, no API bill).

Supported: Claude Code (`claude -p`), Gemini CLI (`gemini -p`), Codex (`codex exec`). Each call:
* runs the CLI headless, once, with the rendered conversation on stdin (`text_protocol`), and turns
  its reply back into tool calls. The CLI never acts on its own: AI-EOS's agent loop and tool gateway
  stay in charge, exactly as with the HTTP adapters;
* starts in a fresh, empty temporary directory, so the CLI cannot see the worker's files;
* gets a reduced environment (what the CLIs need to find their sign-in, nothing else), so database
  URLs, the GitHub App key path and other worker settings are not visible to it;
* asks the CLI to disable its own tools where it supports that (Claude Code `--tools ""`; Codex runs
  in its read-only sandbox). `extra_args` in model-routing.yaml adds flags your CLI version needs.

Model id `default` leaves the model choice to the CLI's own configuration.
Use within the terms of the plan each CLI is signed in with.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aieos.providers.adapters.text_protocol import (
    estimate_tokens,
    parse_reply,
    render_conversation,
    render_system,
)
from aieos.providers.llm import ChatRequest, ChatResponse, ProviderRequestError, ProviderTransientError
from aieos.providers.pricing import TokenUsage

DEFAULT_COMMANDS = {"claude_code": "claude", "gemini_cli": "gemini", "codex_cli": "codex"}
_ENV_KEEP_EXACT = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "SHELL",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
}
_ENV_KEEP_PREFIX = ("XDG_", "CLAUDE_", "ANTHROPIC_", "GEMINI_", "GOOGLE_", "CODEX_", "OPENAI_", "NODE_")
_TRANSIENT_HINTS = (
    "rate limit",
    "rate_limit",
    "429",
    "overloaded",
    "timeout",
    "timed out",
    "503",
    "502",
    "temporarily",
    "try again",
    "econnreset",
    "network",
)


@dataclass(frozen=True, slots=True)
class Completed:
    exit_code: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], bytes, str, dict[str, str], float], Awaitable[Completed]]


async def run_process(argv: list[str], stdin: bytes, cwd: str, env: dict[str, str], timeout: float) -> Completed:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ProviderTransientError(f"{argv[0]}: timed out after {timeout:.0f}s") from None
    return Completed(proc.returncode if proc.returncode is not None else -1, out, err)


def cli_env(source: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if source is None else source
    return {k: v for k, v in env.items() if k in _ENV_KEEP_EXACT or k.startswith(_ENV_KEEP_PREFIX)}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class CliChatModel:
    def __init__(
        self,
        cli: str,
        model: str,
        *,
        command: str | None = None,
        extra_args: tuple[str, ...] = (),
        timeout_seconds: float = 300.0,
        runner: Runner = run_process,
    ) -> None:
        if cli not in DEFAULT_COMMANDS:
            raise ValueError(f"unknown cli {cli!r}")
        self._cli = cli
        self._model = model
        self._command = command or DEFAULT_COMMANDS[cli]
        self._extra = list(extra_args)
        self._timeout = timeout_seconds
        self._run = runner

    def argv(self, system: str, workdir: str) -> list[str]:
        """Exact arguments. The conversation itself always goes through stdin (no argv size limit)."""
        model = self._model != "default"
        if self._cli == "claude_code":
            argv = [self._command, "-p", "--output-format", "json", "--system-prompt", system, "--tools", ""]
            argv += ["--model", self._model] if model else []
        elif self._cli == "gemini_cli":
            # No system-prompt flag: the instructions lead the stdin text; -p adds the final nudge.
            argv = [self._command, "--output-format", "json", "-p", "Reply now, following the instructions above."]
            argv += ["-m", self._model] if model else []
        else:
            argv = [
                self._command,
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-o",
                str(Path(workdir) / "last-message.txt"),
            ]
            argv += ["-m", self._model] if model else []
            argv.append("-")  # prompt from stdin
        return argv + self._extra

    def _stdin(self, request: ChatRequest, system: str) -> str:
        conversation = render_conversation(request)
        if self._cli == "claude_code":
            return conversation  # the system prompt goes through --system-prompt
        return f"{system}\n\n{conversation}"

    def _parse(self, out: Completed, workdir: str, prompt_text: str) -> tuple[str, TokenUsage, str | None]:
        stdout = out.stdout.decode("utf-8", errors="replace")
        if self._cli == "claude_code":
            data = json.loads(stdout)
            if data.get("is_error"):
                raise ProviderRequestError(f"claude: {str(data.get('result'))[:300]}")
            u = data.get("usage") or {}
            read, write = _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))
            usage = TokenUsage(_int(u.get("input_tokens")) + read + write, read, _int(u.get("output_tokens")), write)
            return str(data.get("result") or ""), usage, data.get("session_id")
        if self._cli == "gemini_cli":
            data = json.loads(stdout)
            if data.get("error"):
                raise ProviderRequestError(f"gemini: {str(data['error'])[:300]}")
            text = str(data.get("response") or "")
            inp, out_t, cached = 0, 0, 0
            stats = data.get("stats") or {}
            for model_stats in (stats.get("models") or {}).values():
                tokens = model_stats.get("tokens") or {}
                inp += _int(tokens.get("prompt"))
                out_t += _int(tokens.get("candidates")) + _int(tokens.get("thoughts"))
                cached += _int(tokens.get("cached"))
            if not inp:
                inp, out_t = _int(stats.get("inputTokens")), _int(stats.get("outputTokens"))
            return text, self._usage(inp, cached, out_t, prompt_text, text), None
        # codex: JSON Lines events on stdout, final message in the -o file.
        inp, cached, out_t = 0, 0, 0
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                u = event.get("usage") or {}
                inp += _int(u.get("input_tokens"))
                cached += _int(u.get("cached_input_tokens"))
                out_t += _int(u.get("output_tokens")) + _int(u.get("reasoning_output_tokens"))
            elif event.get("type") == "turn.failed":
                raise ProviderRequestError(f"codex: {str(event.get('error'))[:300]}")
        last = Path(workdir) / "last-message.txt"
        text = last.read_text(encoding="utf-8", errors="replace") if last.exists() else ""
        return text, self._usage(inp, cached, out_t, prompt_text, text), None

    @staticmethod
    def _usage(inp: int, cached: int, out: int, prompt_text: str, reply: str) -> TokenUsage:
        if not inp and not out:  # no usage reported: estimate, so token budgets still apply
            return TokenUsage(estimate_tokens(prompt_text), 0, estimate_tokens(reply))
        return TokenUsage(inp, min(cached, inp), out)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        system = render_system(request)
        stdin = self._stdin(request, system)
        with tempfile.TemporaryDirectory(prefix="aieos-cli-") as workdir:
            try:
                out = await self._run(
                    self.argv(system, workdir), stdin.encode("utf-8"), workdir, cli_env(), self._timeout
                )
            except FileNotFoundError:
                raise ProviderRequestError(f"{self._command}: not installed or not on PATH") from None
            if out.exit_code != 0:
                detail = (out.stderr or out.stdout).decode("utf-8", errors="replace")[-500:]
                transient = any(h in detail.lower() for h in _TRANSIENT_HINTS)
                error = ProviderTransientError if transient else ProviderRequestError
                raise error(f"{self._command} exited with {out.exit_code}: {detail}")
            try:
                text, usage, request_id = self._parse(out, workdir, stdin)
            except (ValueError, KeyError) as exc:
                raise ProviderTransientError(f"{self._command}: unreadable output ({type(exc).__name__})") from None
        message = parse_reply(text, provider=f"cli:{self._cli}")
        return ChatResponse(message, usage, request_id, "tool_use" if message.tool_calls else "end_turn")
