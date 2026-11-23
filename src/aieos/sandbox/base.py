"""Sandbox managers: the operations every backend offers, built on a few backend primitives.

A backend (Docker locally and in CI, ECS Fargate on AWS) provides: create a sandbox, check it exists,
run a command in it (`_exec_raw`), destroy it, and find sandboxes by workflow or age. Everything else
(workspace initialisation, file writes, patch export, safe file reads) is defined once here, so both
backends behave the same and are tested by the same tests.

Isolation guarantees common to all backends: no network to the internet, read-only root filesystem,
commands run as an unprivileged user (uid 65534) without a shell, under a hard timeout, with resource
limits, and with no credentials or secrets in the sandbox.
"""

from __future__ import annotations

import io
import tarfile
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
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
            # A real timestamp: with a fixed one, git treats a same-size rewrite as unchanged (stat cache).
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class SandboxManager(ABC):
    _limits: SandboxLimits
    _ready: set[str]

    # ------------------------------------------------------------------ backend primitives
    @abstractmethod
    async def _create(self, key: SandboxKey) -> None:
        """Start an empty sandbox (workspace writable by uid 65534). Raise SandboxError on failure."""

    @abstractmethod
    async def _exists(self, key: SandboxKey) -> bool: ...

    @abstractmethod
    async def destroy(self, key: SandboxKey) -> None: ...

    @abstractmethod
    async def destroy_workflow(self, workflow_id: UUID) -> int: ...

    @abstractmethod
    async def reap(self, *, older_than_seconds: int) -> int: ...

    @abstractmethod
    async def _exec_raw(
        self,
        key: SandboxKey,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: int | None = None,
        workdir: str = WORKSPACE,
    ) -> Completed:
        """Run argv (no shell) as the sandbox user under a hard timeout."""

    # ------------------------------------------------------------------ shared operations
    async def ensure(
        self,
        key: SandboxKey,
        *,
        seed: dict[str, bytes] | None = None,
        snapshot: Callable[[], Awaitable[bytes]] | None = None,
    ) -> bool:
        """Create the sandbox if needed and initialise /workspace as a git repo with a base commit.

        `snapshot` returns a gzipped repository tarball with one top-level directory (GitHub's
        format); it is fetched only when the sandbox is created, and unpacked inside the sandbox,
        so a hostile archive can only affect the sandbox itself. Returns True if it was created now.
        """
        if key.name in self._ready or await self._exists(key):
            self._ready.add(key.name)
            return False
        await self._create(key)
        try:
            if snapshot is not None:
                await self._extract(key, await snapshot(), gzipped=True, strip_components=1)
            if seed:
                await self._extract(key, _tar(seed))
        except BaseException:
            await self.destroy(key)
            raise
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
        return True

    async def _extract(
        self, key: SandboxKey, tar_bytes: bytes, *, gzipped: bool = False, strip_components: int = 0
    ) -> None:
        # Commands run with the workspace as working directory, on every backend.
        argv = ["tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", "."]
        if gzipped:
            argv.append("-z")
        if strip_components:
            argv.append(f"--strip-components={strip_components}")
        r = await self._exec_raw(key, argv, stdin=tar_bytes, timeout=300)
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

    async def run(
        self, key: SandboxKey, argv: list[str], *, stdin: bytes | None = None, timeout: int = 120
    ) -> Completed:
        """Raw command for platform-controlled steps (not agent commands): output is not truncated."""
        return await self._exec_raw(key, argv, stdin=stdin, timeout=timeout)

    async def read_files(self, key: SandboxKey, paths: list[str], *, max_bytes: int) -> dict[str, tuple[bytes, bool]]:
        """Read regular files out of the workspace: {path: (content, executable)}.

        The tar stream comes from an untrusted container, so it is parsed in memory and only
        regular files whose names were requested are accepted; links, devices and extra members fail.
        """
        wanted = {safe_relative_path(p) for p in paths}
        if not wanted:
            return {}
        r = await self._exec_raw(key, ["tar", "-c", "--no-recursion", "-C", ".", "--", *sorted(wanted)], timeout=120)
        if r.exit_code != 0:
            raise SandboxError(f"could not read files: {r.stderr.decode(errors='replace')[:300]}")
        files: dict[str, tuple[bytes, bool]] = {}
        total = 0
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:") as tar:
            for member in tar:
                if member.name not in wanted or member.name in files:
                    raise SandboxError(f"unexpected archive member {member.name!r}")
                if not member.isreg():
                    raise SandboxError(f"{member.name} is not a regular file")
                total += member.size
                if total > max_bytes:
                    raise SandboxError(f"changed files exceed {max_bytes} bytes")
                fh = tar.extractfile(member)
                assert fh is not None
                files[member.name] = (fh.read(), bool(member.mode & 0o100))
        if set(files) != wanted:
            raise SandboxError(f"missing files: {sorted(wanted - set(files))}")
        return files

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
