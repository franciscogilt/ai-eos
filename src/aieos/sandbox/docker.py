"""Disposable Docker sandboxes for agent-generated code (local development and CI; AWS: sandbox/fargate.py).

The container is the security boundary, not the command allowlist. Every sandbox:
  * has no network (`--network none`),
  * has a read-only root filesystem; only /workspace and /tmp are writable (size-capped tmpfs),
  * runs as an unprivileged user with all capabilities dropped and no-new-privileges,
  * is limited in CPU, memory (swap included) and process count,
  * gets no credentials, environment secrets or host mounts,
  * carries labels (org, workflow, agent run) so it can always be found and removed.
Commands run without a shell, under an in-container `timeout -s KILL` plus a host-side deadline.
The Docker CLI is called directly (no SDK dependency). Whoever can reach the Docker socket is root on
the host, so only the tool-gateway worker gets it, and only for local development and CI.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

from aieos.core.logging import get_logger
from aieos.sandbox.base import (
    GIT_IDENTITY,
    SAFE_DIFF,
    SANDBOX_UID,
    WORKSPACE,
    Completed,
    ExecResult,
    SandboxError,
    SandboxKey,
    SandboxLimits,
    SandboxManager,
    UnsafePathError,
    safe_relative_path,
)

__all__ = [
    "GIT_IDENTITY",
    "SAFE_DIFF",
    "WORKSPACE",
    "Completed",
    "DockerCLI",
    "DockerSandboxManager",
    "ExecResult",
    "SandboxError",
    "SandboxKey",
    "SandboxLimits",
    "SandboxManager",
    "UnsafePathError",
    "safe_relative_path",
]

log = get_logger(__name__)


class DockerCLI:
    """Runs `docker` without a shell. Replaceable in unit tests."""

    def __init__(self, binary: str = "docker") -> None:
        self._binary = binary

    async def run(self, args: list[str], *, stdin: bytes | None = None, timeout: float = 60.0) -> Completed:
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            return Completed(-1, out, err, timed_out=True)
        return Completed(proc.returncode if proc.returncode is not None else -1, out, err, timed_out=False)


class DockerSandboxManager(SandboxManager):
    def __init__(
        self, cli: DockerCLI, *, image: str, limits: SandboxLimits | None = None, runtime: str | None = None
    ) -> None:
        self._cli = cli
        self._image = image
        self._limits = limits or SandboxLimits()
        self._runtime = runtime  # e.g. "runsc" (gVisor) where installed
        self._ready: set[str] = set()

    # ------------------------------------------------------------------ container lifecycle
    def run_args(self, key: SandboxKey) -> list[str]:
        """Exact `docker run` arguments. Pure, so the isolation settings are unit-testable."""
        lim = self._limits
        args = [
            "run",
            "--detach",
            "--name",
            key.name,
            "--init",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            f"{WORKSPACE}:rw,exec,nosuid,nodev,size={lim.workspace_mb}m,uid=65534,gid=65534,mode=0700",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={lim.tmp_mb}m,mode=1777",  # noqa: S108 - container path
            "--user",
            SANDBOX_UID,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(lim.pids),
            "--memory",
            f"{lim.memory_mb}m",
            "--memory-swap",
            f"{lim.memory_mb}m",
            "--cpus",
            str(lim.cpus),
            "--workdir",
            WORKSPACE,
            "--env",
            "HOME=/tmp",
            "--label",
            "aieos.sandbox=1",
            "--label",
            f"aieos.org_id={key.org_id}",
            "--label",
            f"aieos.workflow_id={key.workflow_id}",
            "--label",
            f"aieos.agent_run_id={key.agent_run_id}",
            "--label",
            f"aieos.created_at={int(time.time())}",
        ]
        if self._runtime:
            args += ["--runtime", self._runtime]
        return [*args, self._image, "sleep", "infinity"]

    async def _docker(self, args: list[str], *, stdin: bytes | None = None, timeout: float = 120.0) -> Completed:
        result = await self._cli.run(args, stdin=stdin, timeout=timeout)
        if result.timed_out:
            raise SandboxError(f"docker {args[0]} timed out")
        return result

    async def _create(self, key: SandboxKey) -> None:
        created = await self._docker(self.run_args(key))
        if created.exit_code != 0:
            raise SandboxError(f"could not start sandbox: {created.stderr.decode(errors='replace')[:500]}")

    async def _exists(self, key: SandboxKey) -> bool:
        r = await self._docker(["inspect", "--format", "{{.State.Running}}", key.name])
        return r.exit_code == 0 and r.stdout.strip() == b"true"

    async def destroy(self, key: SandboxKey) -> None:
        await self._docker(["rm", "--force", key.name])
        self._ready.discard(key.name)

    async def destroy_workflow(self, workflow_id: UUID) -> int:
        """Remove every sandbox of a workflow (cancel, failure, completion). Returns how many."""
        listed = await self._docker(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=aieos.sandbox=1",
                "--filter",
                f"label=aieos.workflow_id={workflow_id}",
            ]
        )
        ids = listed.stdout.decode().split()
        if ids:
            await self._docker(["rm", "--force", *ids])
        self._ready = {n for n in self._ready if n not in ids}
        log.info("sandbox.workflow_cleaned", workflow_id=str(workflow_id), removed=len(ids))
        return len(ids)

    async def reap(self, *, older_than_seconds: int) -> int:
        """Backstop for crashed workers: remove sandboxes older than the limit."""
        listed = await self._docker(
            ["ps", "--all", "--filter", "label=aieos.sandbox=1", "--format", '{{.ID}} {{.Label "aieos.created_at"}}']
        )
        cutoff = time.time() - older_than_seconds
        stale = [
            cid
            for cid, _, ts in (line.partition(" ") for line in listed.stdout.decode().splitlines())
            if ts.isdigit() and int(ts) < cutoff
        ]
        if stale:
            await self._docker(["rm", "--force", *stale])
        return len(stale)

    # ------------------------------------------------------------------ operations
    async def _exec_raw(
        self,
        key: SandboxKey,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: int | None = None,
        workdir: str = WORKSPACE,
    ) -> Completed:
        seconds = int(timeout or self._limits.command_timeout_seconds)
        args = [
            "exec",
            *(["--interactive"] if stdin is not None else []),
            "--workdir",
            workdir,
            key.name,
            "timeout",
            "--signal=KILL",
            str(seconds),
            *argv,
        ]
        # Host-side deadline a little after the in-container one, in case `timeout` itself is stuck.
        return await self._cli.run(args, stdin=stdin, timeout=seconds + 15)
