"""Sandbox manager and handlers with a fake Docker CLI: isolation settings, validation, call sequences."""

from __future__ import annotations

import asyncio
import io
import itertools
import tarfile
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from aieos.gateway.handlers import ToolInputError
from aieos.gateway.sandbox_handlers import DEFAULT_ALLOWED_COMMANDS, _argv, sandbox_handlers
from aieos.sandbox.docker import (
    SAFE_DIFF,
    Completed,
    DockerSandboxManager,
    SandboxError,
    SandboxKey,
    SandboxLimits,
    UnsafePathError,
    _tar,
    safe_relative_path,
)


class FakeCLI:
    def __init__(self, responses: dict[str, Completed] | None = None) -> None:
        self.calls: list[tuple[list[str], bytes | None, float]] = []
        self.responses = responses or {}

    async def run(self, args: list[str], *, stdin: bytes | None = None, timeout: float = 60.0) -> Completed:
        self.calls.append((args, stdin, timeout))
        joined = " ".join(args)
        for fragment, result in self.responses.items():
            if fragment in joined:
                return result
        if args[0] == "inspect":
            return Completed(1, b"", b"no such container", False)
        return Completed(0, b"", b"", False)


def key() -> SandboxKey:
    return SandboxKey(org_id=uuid.uuid4(), workflow_id=uuid.uuid4(), agent_run_id=uuid.uuid4())


def manager(cli: FakeCLI, **kw: Any) -> DockerSandboxManager:
    return DockerSandboxManager(cli, image="aieos-sandbox:test", **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ isolation settings
def test_run_args_enforce_isolation():
    k = key()
    args = manager(FakeCLI(), limits=SandboxLimits(cpus=1.5, memory_mb=512, pids=64)).run_args(k)
    pairs = set(itertools.pairwise(args))
    assert ("--network", "none") in pairs
    assert "--read-only" in args and ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges") in pairs and ("--user", "65534:65534") in pairs
    assert ("--memory", "512m") in pairs and ("--memory-swap", "512m") in pairs  # no swap escape
    assert ("--pids-limit", "64") in pairs and ("--cpus", "1.5") in pairs
    workspace = args[args.index("--tmpfs") + 1]
    # Owned by the sandbox user, private, so git's ownership check passes without disabling it.
    assert workspace.startswith("/workspace:") and "uid=65534,gid=65534,mode=0700" in workspace
    assert ("--label", f"aieos.workflow_id={k.workflow_id}") in pairs
    for forbidden in ("--privileged", "-v", "--volume", "--mount", "--network=host", "--pid", "--ipc"):
        assert forbidden not in args
    assert [a for a in args if a.startswith("HOME=") or a == "--env"] == ["--env", "HOME=/tmp"]  # no secrets passed
    assert args[-3:] == ["aieos-sandbox:test", "sleep", "infinity"]


def test_optional_gvisor_runtime():
    args = manager(FakeCLI(), runtime="runsc").run_args(key())
    assert args[args.index("--runtime") + 1] == "runsc"
    assert "--runtime" not in manager(FakeCLI()).run_args(key())


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x", "a/../../b", ".git/hooks/pre-commit", "x\x00y"])
def test_unsafe_paths_rejected(bad):
    with pytest.raises(UnsafePathError):
        safe_relative_path(bad)


def test_safe_paths_and_tar():
    assert safe_relative_path("src/app.py") == "src/app.py"
    assert safe_relative_path("src//app.py") == "src/app.py"  # normalised, still inside the workspace
    with tarfile.open(fileobj=io.BytesIO(_tar({"src/a.py": b"x = 1\n"}))) as tar:
        (member,) = tar.getmembers()
        assert member.name == "src/a.py" and member.mode == 0o644 and member.uid == 0
    with pytest.raises(UnsafePathError):
        _tar({"../escape": b""})


# ------------------------------------------------------------------ lifecycle and operations
def test_ensure_creates_once_and_initialises_git():
    cli = FakeCLI()
    m, k = manager(cli), key()
    asyncio.run(m.ensure(k, seed={"README.md": b"hi"}))
    asyncio.run(m.ensure(k))
    verbs = [c[0][0] for c in cli.calls]
    assert verbs.count("run") == 1
    execs = [c[0] for c in cli.calls if c[0][0] == "exec"]
    assert execs[0][-6:] == ["tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", "/workspace"]
    assert [e[e.index("timeout") + 3 :][:2] for e in execs[1:]] == [["git", "init"], ["git", "add"], ["git", "-c"]]


def test_failed_start_raises():
    cli = FakeCLI({"run --detach": Completed(125, b"", b"image not found", False)})
    with pytest.raises(SandboxError, match="image not found"):
        asyncio.run(manager(cli).ensure(key()))


def test_exec_is_time_bounded_and_truncated():
    cli = FakeCLI({"exec": Completed(0, b"x" * 100, b"", False)})
    m, k = manager(cli, limits=SandboxLimits(max_output_bytes=10)), key()
    result = asyncio.run(m.exec(k, ["python", "-c", "print(1)"], timeout=7))
    args, _, host_timeout = cli.calls[-1]
    assert args[args.index("timeout") : args.index("timeout") + 3] == ["timeout", "--signal=KILL", "7"]
    assert host_timeout > 7 and result.truncated and "90 bytes truncated" in result.stdout
    with pytest.raises(UnsafePathError):
        asyncio.run(m.exec(k, ["ls"], workdir="../../etc"))


def test_export_patch_disables_repository_controlled_diff():
    cli = FakeCLI({"exec": Completed(0, b"diff --git a/x b/x", b"", False)})
    patch, _ = asyncio.run(manager(cli).export_patch(key()))
    diff_call = next(c[0] for c in cli.calls if "--text" in c[0])
    tail = diff_call[diff_call.index("git") + 1 :]
    assert tail[: len(SAFE_DIFF)] == SAFE_DIFF
    assert {"--no-ext-diff", "--no-textconv", "--text"} <= set(tail) and "--binary" not in tail
    assert patch == b"diff --git a/x b/x"


def test_destroy_workflow_targets_labels():
    wf = uuid.uuid4()
    cli = FakeCLI({"ps --all": Completed(0, b"abc\ndef\n", b"", False)})
    assert asyncio.run(manager(cli).destroy_workflow(wf)) == 2
    ps = next(c[0] for c in cli.calls if c[0][0] == "ps")
    assert f"label=aieos.workflow_id={wf}" in ps and "label=aieos.sandbox=1" in ps
    assert cli.calls[-1][0] == ["rm", "--force", "abc", "def"]


def test_reap_removes_only_stale():
    now = int(time.time())
    listing = f"old1 {now - 10_000}\nnew1 {now - 5}\nweird \n".encode()
    cli = FakeCLI({"ps --all": Completed(0, listing, b"", False)})
    assert asyncio.run(manager(cli).reap(older_than_seconds=3600)) == 1
    assert cli.calls[-1][0] == ["rm", "--force", "old1"]


# ------------------------------------------------------------------ handlers
def test_command_allowlist():
    assert _argv("pytest -q 'tests/test a.py'", DEFAULT_ALLOWED_COMMANDS) == ["pytest", "-q", "tests/test a.py"]
    for bad in ["rm -rf /", "/bin/sh -c id", "bash", "curl http://x", "", "python 'unterminated"]:
        with pytest.raises(ToolInputError):
            _argv(bad, DEFAULT_ALLOWED_COMMANDS)


class FakeStore:
    def __init__(self) -> None:
        self.puts: list[bytes] = []

    async def put(self, org_id, data, **kw):
        self.puts.append(data)
        return SimpleNamespace(id=uuid.uuid4(), sha256="0" * 64, size_bytes=len(data))


def ctx() -> Any:
    return SimpleNamespace(org_id=uuid.uuid4(), workflow_id=uuid.uuid4(), agent_run_id=uuid.uuid4())


def test_handlers_validate_and_store_patches():
    cli = FakeCLI()
    store = FakeStore()
    h = sandbox_handlers(manager(cli), store)  # type: ignore[arg-type]
    c = ctx()
    with pytest.raises(ToolInputError):
        asyncio.run(h["sandbox.write_file"](c, {"path": ".git/config", "content": "x"}))
    with pytest.raises(ToolInputError):
        asyncio.run(h["sandbox.write_file"](c, {"path": "big.txt", "content": "x" * (1024 * 1024 + 1)}))
    with pytest.raises(ToolInputError, match="selector"):
        asyncio.run(h["sandbox.run_tests"](c, {"selector": "tests; rm -rf /"}))
    ok = asyncio.run(h["sandbox.write_file"](c, {"path": "src/app.py", "content": "x = 1\n"}))
    assert ok == {"path": "src/app.py", "bytes": 6}

    empty = asyncio.run(h["sandbox.export_patch"](c, {"summary": "nothing"}))
    assert empty == {"empty": True, "files_changed": []} and store.puts == []
    cli.responses["exec"] = Completed(0, b"diff --git a/src/app.py b/src/app.py\n", b"", False)
    stored = asyncio.run(h["sandbox.export_patch"](c, {"summary": "add app"}))
    assert stored["empty"] is False and store.puts == [b"diff --git a/src/app.py b/src/app.py\n"]


def test_run_tests_reports_pass_fail():
    cli = FakeCLI({"pytest -q": Completed(1, b"1 failed", b"", False)})
    h = sandbox_handlers(manager(cli), FakeStore())  # type: ignore[arg-type]
    result = asyncio.run(h["sandbox.run_tests"](ctx(), {"selector": "tests/test_x.py -k export"}))
    assert result["passed"] is False and result["exit_code"] == 1
    argv = cli.calls[-1][0]
    start = argv.index("python")
    assert argv[start:] == ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_x.py", "-k", "export"]
