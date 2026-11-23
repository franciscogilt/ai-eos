"""Sandbox tools (Level 2). They run in the gateway worker, which alone talks to Docker.

One sandbox per agent run, created on first use and found again by its name after a worker
restart. The command allowlist is hygiene (it keeps agents on task); the container is the
security boundary, because tests themselves execute arbitrary repository code.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from aieos.artifacts.store import ArtifactStore
from aieos.gateway.handlers import Handler, ToolContext, ToolInputError
from aieos.sandbox.docker import DockerSandboxManager, SandboxKey, UnsafePathError

DEFAULT_ALLOWED_COMMANDS = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "ruff",
        "mypy",
        "git",
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "wc",
        "diff",
        "make",
        "echo",
        "true",
    }
)
MAX_FILE_BYTES = 1024 * 1024
_SELECTOR = re.compile(r"^[A-Za-z0-9_./:\[\]\-=, ]{1,300}$")


def _key(ctx: ToolContext) -> SandboxKey:
    return SandboxKey(org_id=ctx.org_id, workflow_id=ctx.workflow_id, agent_run_id=ctx.agent_run_id)


def _argv(command: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ToolInputError("command (non-empty string) is required")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolInputError(f"cannot parse command: {exc}") from None
    program = argv[0]
    if "/" in program or program not in allowed:
        raise ToolInputError(f"{program!r} is not an allowed command; allowed: {', '.join(sorted(allowed))}")
    return argv


def _exec_result(r: Any) -> dict[str, Any]:
    return {
        "exit_code": r.exit_code,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "timed_out": r.timed_out,
        "duration_ms": r.duration_ms,
        "truncated": r.truncated,
    }


Snapshot = Callable[[ToolContext], Awaitable[bytes]]


def sandbox_handlers(
    manager: DockerSandboxManager,
    store: ArtifactStore,
    *,
    allowed_commands: frozenset[str] = DEFAULT_ALLOWED_COMMANDS,
    snapshot: Snapshot | None = None,
) -> dict[str, Handler]:
    """`snapshot` (B13) seeds a new sandbox with the workflow's repository at its base commit;
    without it a sandbox starts empty."""

    async def ensure(ctx: ToolContext) -> None:
        source = snapshot
        await manager.ensure(_key(ctx), snapshot=(lambda: source(ctx)) if source is not None else None)

    async def write_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        path, content = args.get("path"), args.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ToolInputError("path (str) and content (str) are required")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ToolInputError(f"file larger than {MAX_FILE_BYTES} bytes")
        await ensure(ctx)
        try:
            written = await manager.write_file(_key(ctx), path, data)
        except UnsafePathError as exc:
            raise ToolInputError(str(exc)) from None
        return {"path": written, "bytes": len(data)}

    async def run_command(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        argv = _argv(args.get("command"), allowed_commands)
        await ensure(ctx)
        try:
            result = await manager.exec(_key(ctx), argv, workdir=args.get("working_dir") or None)
        except UnsafePathError as exc:
            raise ToolInputError(str(exc)) from None
        return _exec_result(result)

    async def run_tests(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        selector = args.get("selector")
        argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if selector:
            if not isinstance(selector, str) or not _SELECTOR.match(selector):
                raise ToolInputError("selector may contain only test paths, node ids and -k/-m expressions")
            argv += shlex.split(selector)
        await ensure(ctx)
        result = _exec_result(await manager.exec(_key(ctx), argv))
        result["passed"] = result["exit_code"] == 0 and not result["timed_out"]
        return result

    async def export_patch(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        await ensure(ctx)
        patch, files = await manager.export_patch(_key(ctx))
        if not patch:
            return {"empty": True, "files_changed": []}
        row = await store.put(ctx.org_id, patch, kind="patch", content_type="text/x-diff", workflow_id=ctx.workflow_id)
        return {
            "empty": False,
            "artifact_id": str(row.id),
            "sha256": row.sha256,
            "size_bytes": row.size_bytes,
            "files_changed": files,
            "summary": str(args.get("summary", ""))[:500],
        }

    return {
        "sandbox.write_file": write_file,
        "sandbox.run_command": run_command,
        "sandbox.run_tests": run_tests,
        "sandbox.export_patch": export_patch,
    }
