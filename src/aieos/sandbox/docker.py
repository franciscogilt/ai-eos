"""Disposable Docker sandboxes for agent-generated code (local and CI; AWS uses Fargate later).

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
import io
import tarfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from aieos.core.logging import get_logger

log = get_logger(__name__)

WORKSPACE = "/workspace"
SANDBOX_UID = "65534:65534"  # nobody
GIT_IDENTITY = ["-c", "user.name=aieos-sandbox", "-c", "user.email=sandbox@aieos.invalid"]
# Neutralises repository-controlled git behaviour when we read the diff, so code in the sandbox cannot
# hide or forge changes in the exported patch: no external diff drivers, no textconv filters, and
# `--text` so a `.gitattributes` entry such as `*.py -diff` cannot turn a source change into an opaque
# "Binary files differ" line. Genuinely binary files therefore appear as raw text; the push path (B13)
# rejects patches that touch binary files or .gitattributes.
SAFE_DIFF = [
    "-c",
    "diff.external=",
    "-c",
    "core.attributesFile=/dev/null",
    "diff",
    "--cached",
    "--text",
    "--no-ext-diff",
    "--no-textconv",
]


class SandboxError(RuntimeError):
    pass


class UnsafePathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    cpus: float = 2.0
    memory_mb: int = 2048
    pids: int = 256
    workspace_mb: int = 1024
    tmp_mb: int = 256
    command_timeout_seconds: int = 900
    max_output_bytes: int = 16 * 1024


@dataclass(frozen=True, slots=True)
class SandboxKey:
    org_id: UUID
    workflow_id: UUID
    agent_run_id: UUID

    @property
    def name(self) -> str:
        return f"aieos-sbx-{self.agent_run_id}"


@dataclass(frozen=True, slots=True)
class Completed:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    truncated: bool


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


def safe_relative_path(path: str) -> str:
    """A workspace-relative path that cannot escape /workspace or touch git internals."""
    if not path or "\x00" in path:
        raise UnsafePathError("empty path")
    p = PurePosixPath(path)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise UnsafePathError(f"path must be relative and inside the workspace: {path!r}")
    if p.parts[0] == ".git":
        raise UnsafePathError("writing to .git is not allowed")
    return str(p)


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    return data[:limit].decode("utf-8", errors="replace") + f"\n[... {len(data) - limit} bytes truncated]", True


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(safe_relative_path(name))
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class DockerSandboxManager:
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
            f"{WORKSPACE}:rw,exec,nosuid,nodev,size={lim.workspace_mb}m,mode=1777",
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

    async def _exists(self, key: SandboxKey) -> bool:
        r = await self._docker(["inspect", "--format", "{{.State.Running}}", key.name])
        return r.exit_code == 0 and r.stdout.strip() == b"true"

    async def ensure(self, key: SandboxKey, *, seed: dict[str, bytes] | None = None) -> None:
        """Create the sandbox if needed and initialise /workspace as a git repo with a base commit."""
        if key.name in self._ready or await self._exists(key):
            self._ready.add(key.name)
            return
        created = await self._docker(self.run_args(key))
        if created.exit_code != 0:
            raise SandboxError(f"could not start sandbox: {created.stderr.decode(errors='replace')[:500]}")
        if seed:
            await self._extract(key, _tar(seed))
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "aieos base"],
        ):
            r = await self._exec_raw(key, argv, timeout=60)
            if r.exit_code != 0:
                await self.destroy(key)
                raise SandboxError(f"workspace init failed at {argv[1]}: {r.stderr.decode(errors='replace')[:300]}")
        self._ready.add(key.name)
        log.info("sandbox.created", sandbox=key.name, workflow_id=str(key.workflow_id))

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

    async def _extract(self, key: SandboxKey, tar_bytes: bytes) -> None:
        r = await self._exec_raw(
            key,
            ["tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", WORKSPACE],
            stdin=tar_bytes,
            timeout=120,
        )
        if r.exit_code != 0:
            raise SandboxError(f"could not write files: {r.stderr.decode(errors='replace')[:300]}")

    async def write_file(self, key: SandboxKey, path: str, content: bytes) -> str:
        rel = safe_relative_path(path)
        await self._extract(key, _tar({rel: content}))
        return rel

    async def exec(
        self, key: SandboxKey, argv: list[str], *, workdir: str | None = None, timeout: int | None = None
    ) -> ExecResult:
        directory = WORKSPACE if not workdir else f"{WORKSPACE}/{safe_relative_path(workdir)}"
        started = time.monotonic()
        r = await self._exec_raw(key, argv, workdir=directory, timeout=timeout)
        out, t1 = _truncate(r.stdout, self._limits.max_output_bytes)
        err, t2 = _truncate(r.stderr, self._limits.max_output_bytes)
        # 137 = killed by SIGKILL: the in-container timeout, or the memory limit.
        timed_out = r.timed_out or (
            r.exit_code == 137 and time.monotonic() - started >= (timeout or self._limits.command_timeout_seconds)
        )
        return ExecResult(r.exit_code, out, err, timed_out, int((time.monotonic() - started) * 1000), t1 or t2)

    async def export_patch(self, key: SandboxKey) -> tuple[bytes, list[str]]:
        """The workspace diff against the base commit, read with repository-controlled git behaviour disabled."""
        await self._exec_raw(key, ["git", "add", "-A"], timeout=120)
        diff = await self._exec_raw(key, ["git", *SAFE_DIFF, "HEAD"], timeout=120)
        names = await self._exec_raw(
            key, ["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-only", "HEAD"], timeout=60
        )
        if diff.exit_code != 0:
            raise SandboxError(f"could not export patch: {diff.stderr.decode(errors='replace')[:300]}")
        return diff.stdout, names.stdout.decode(errors="replace").split()
