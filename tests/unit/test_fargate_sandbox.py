"""Fargate sandbox backend, end to end on this machine.

A stand-in ECS client starts the real executor (sandbox/executor.py) as a local process for each
`run_task`, on its own loopback address, so the manager, the wire format, the token check and the
shared sandbox operations (git workspace, file writes, patch export, file reads) all run for real.
Privilege dropping to uid 65534 needs root and is covered by tests/sandbox (Docker, in CI).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from aieos.sandbox.base import SandboxError, SandboxKey, SandboxLimits
from aieos.sandbox.executor import frame
from aieos.sandbox.fargate import FargateSandboxManager

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SECRET = b"s" * 32
EXECUTOR = Path(__file__).resolve().parents[2] / "src" / "aieos" / "sandbox" / "executor.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalEcs:
    """Just enough of ECS: each task is a local executor process on 127.0.0.N:<port>."""

    def __init__(self, tmp: Path, port: int, *, fail_start: bool = False) -> None:
        self.tmp, self.port, self.fail_start = tmp, port, fail_start
        self.tasks: dict[str, dict[str, Any]] = {}
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.counter = itertools.count(2)
        self.run_requests: list[dict[str, Any]] = []

    def run_task(self, **kw: Any) -> dict[str, Any]:
        self.run_requests.append(kw)
        arn = f"arn:aws:ecs:us-east-1:1:task/c/{uuid.uuid4().hex}"
        ip = f"127.0.0.{next(self.counter)}"
        env = {e["name"]: e["value"] for e in kw["overrides"]["containerOverrides"][0]["environment"]}
        workspace = self.tmp / arn.rsplit("/", 1)[-1]
        workspace.mkdir()
        status = "STOPPED" if self.fail_start else "RUNNING"
        if not self.fail_start:
            self.procs[arn] = subprocess.Popen(
                [sys.executable, str(EXECUTOR)],
                env={
                    "PATH": os.environ["PATH"],
                    "AIEOS_EXECUTOR_TOKEN": env["AIEOS_EXECUTOR_TOKEN"],
                    "AIEOS_WORKSPACE": str(workspace),
                    "AIEOS_EXECUTOR_PORT": str(self.port),
                    "AIEOS_EXECUTOR_BIND": ip,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.tasks[arn] = {
            "taskArn": arn,
            "lastStatus": status,
            "stoppedReason": "CannotPullContainerError" if self.fail_start else None,
            "startedBy": kw["startedBy"],
            "tags": kw["tags"],
            "attachments": [{"details": [{"name": "privateIPv4Address", "value": ip}]}],
            "workspace": workspace,
        }
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def describe_tasks(self, **kw: Any) -> dict[str, Any]:
        return {"tasks": [self.tasks[a] for a in kw["tasks"] if a in self.tasks]}

    def list_tasks(self, **kw: Any) -> dict[str, Any]:
        found = [
            a
            for a, t in self.tasks.items()
            if t["lastStatus"] == "RUNNING" and ("startedBy" not in kw or t["startedBy"] == kw["startedBy"])
        ]
        return {"taskArns": found}

    def stop_task(self, **kw: Any) -> dict[str, Any]:
        task = self.tasks[kw["task"]]
        task["lastStatus"] = "STOPPED"
        proc = self.procs.pop(kw["task"], None)
        if proc:
            proc.kill()
            proc.wait()
        return {}

    def close(self) -> None:
        for proc in self.procs.values():
            proc.kill()
            proc.wait()


@pytest.fixture
def base_dir():
    # Traversable by uid 65534: when these tests run as root, the executor drops to that user,
    # exactly as in a Fargate task (pytest's own tmp_path is private to root).
    path = Path(tempfile.mkdtemp(prefix="aieos-fargate-"))
    path.chmod(0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def ecs(base_dir):
    e = LocalEcs(base_dir, free_port())
    yield e
    e.close()


def manager(ecs: LocalEcs, **kw: Any) -> FargateSandboxManager:
    return FargateSandboxManager(
        ecs,
        cluster="c",
        task_definition="arn:aws:ecs:us-east-1:1:task-definition/aieos-sandbox:3",
        subnets=["subnet-iso"],
        security_groups=["sg-sbx"],
        token_secret=SECRET,
        port=ecs.port,
        poll_seconds=0.1,
        startup_timeout_seconds=kw.pop("startup", 15),
        limits=SandboxLimits(cpus=1.0, memory_mb=2048, command_timeout_seconds=30),
        **kw,
    )


def key(workflow: uuid.UUID | None = None) -> SandboxKey:
    return SandboxKey(org_id=uuid.uuid4(), workflow_id=workflow or uuid.uuid4(), agent_run_id=uuid.uuid4())


def test_workspace_lifecycle_through_the_executor(ecs):
    m, k = manager(ecs), key()

    async def scenario():
        assert await m.ensure(k, seed={"src/app.py": b"x = 1\n"}) is True
        assert await m.ensure(k) is False  # already running
        await m.write_file(k, "src/app.py", b"x = 2\n")
        await m.write_file(k, "tests/test_app.py", b"def test(): pass\n")
        ran = await m.exec(k, ["cat", "src/app.py"])
        patch, files = await m.export_patch(k)
        read = await m.read_files(k, ["src/app.py"], max_bytes=1000)
        env = await m.exec(k, ["env"])
        return ran, patch, files, read, env

    ran, patch, files, read, env = asyncio.run(scenario())
    assert ran.exit_code == 0 and ran.stdout == "x = 2\n"
    assert b"-x = 1" in patch and b"+x = 2" in patch and files == ["src/app.py", "tests/test_app.py"]
    assert read == {"src/app.py": (b"x = 2\n", False)}
    assert "AIEOS_EXECUTOR_TOKEN" not in env.stdout and "HOME=/tmp" in env.stdout  # clean environment

    req = ecs.run_requests[0]
    assert req["launchType"] == "FARGATE" and req["startedBy"] == k.name and req["enableExecuteCommand"] is False
    net = req["networkConfiguration"]["awsvpcConfiguration"]
    assert net == {"subnets": ["subnet-iso"], "securityGroups": ["sg-sbx"], "assignPublicIp": "DISABLED"}
    assert {t["key"] for t in req["tags"]} >= {"aieos.sandbox", "aieos.workflow_id", "aieos.agent_run_id"}
    assert req["overrides"]["cpu"] == "1024" and req["overrides"]["memory"] == "2048"


def test_timeouts_kill_the_command(ecs):
    m, k = manager(ecs), key()

    async def scenario():
        await m.ensure(k)
        started = time.monotonic()
        result = await m.exec(k, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result.timed_out and result.exit_code == 137 and elapsed < 10


def test_token_is_required_and_per_sandbox(ecs):
    m, k = manager(ecs), key()
    asyncio.run(m.ensure(k))
    assert m.token(k) != m.token(key()) and m.token(k) == manager(ecs).token(k)  # derivable, not guessable
    address = ecs.tasks[next(iter(ecs.tasks))]["attachments"][0]["details"][0]["value"]
    body = frame({"argv": ["id"], "timeout": 5, "workdir": ""}, b"")
    for auth in (None, "Bearer wrong", f"Bearer {m.token(key())}"):
        headers = {"Authorization": auth} if auth else {}
        r = httpx.post(f"http://{address}:{ecs.port}/exec", content=body, headers=headers)
        assert r.status_code == 401


def test_workdir_outside_the_workspace_is_refused(ecs):
    m, k = manager(ecs), key()

    async def scenario():
        await m.ensure(k)
        return await m._exec_raw(k, ["ls"], workdir="/etc")

    with pytest.raises(SandboxError, match="inside the workspace"):
        asyncio.run(scenario())


def test_a_restarted_gateway_reaches_the_same_sandbox(ecs):
    k = key()
    asyncio.run(manager(ecs).ensure(k, seed={"a.txt": b"kept\n"}))
    fresh = manager(ecs)  # new process: no local state, same secret

    async def scenario():
        assert await fresh.ensure(k) is False  # found by startedBy, not recreated
        return await fresh.exec(k, ["cat", "a.txt"])

    assert asyncio.run(scenario()).stdout == "kept\n" and len(ecs.run_requests) == 1


def test_cleanup_by_workflow_and_age(ecs):
    wf = uuid.uuid4()
    m = manager(ecs)
    keys = [key(wf), key(wf), key()]

    async def scenario():
        for k in keys:
            await m.ensure(k)
        removed = await m.destroy_workflow(wf)
        return removed

    assert asyncio.run(scenario()) == 2
    running = [t for t in ecs.tasks.values() if t["lastStatus"] == "RUNNING"]
    assert len(running) == 1 and running[0]["startedBy"] == keys[2].name
    for tag in running[0]["tags"]:
        if tag["key"] == "aieos.created_at":
            tag["value"] = str(int(time.time()) - 10_000)
    assert asyncio.run(m.reap(older_than_seconds=3600)) == 1


def test_a_task_that_stops_while_starting_is_reported(base_dir):
    failing = LocalEcs(base_dir, free_port(), fail_start=True)
    with pytest.raises(SandboxError, match="CannotPullContainerError"):
        asyncio.run(manager(failing).ensure(key()))


def test_short_secret_is_refused(ecs):
    with pytest.raises(ValueError):
        FargateSandboxManager(
            ecs, cluster="c", task_definition="td", subnets=[], security_groups=[], token_secret=b"short"
        )


def test_executor_refuses_paths_that_escape(ecs):
    m, k = manager(ecs), key()
    asyncio.run(m.ensure(k))
    address = ecs.tasks[next(iter(ecs.tasks))]["attachments"][0]["details"][0]["value"]
    for workdir in ("/etc", "../..", "a/../../.."):
        body = frame({"argv": ["ls"], "timeout": 5, "workdir": workdir}, b"")
        r = httpx.post(
            f"http://{address}:{ecs.port}/exec", content=body, headers={"Authorization": f"Bearer {m.token(k)}"}
        )
        assert r.status_code == 400 and b"inside the workspace" in r.content


@pytest.mark.skipif(os.geteuid() != 0, reason="privilege dropping needs root (as in the Fargate task)")
def test_commands_run_as_the_sandbox_user_and_cannot_touch_the_executor(ecs):
    m, k = manager(ecs), key()

    async def scenario():
        await m.ensure(k)
        whoami = await m.exec(k, ["id", "-u"])
        kill = await m.exec(k, ["kill", "-9", str(next(iter(ecs.procs.values())).pid)])
        still = await m.exec(k, ["true"])
        return whoami, kill, still

    whoami, kill, still = asyncio.run(scenario())
    assert whoami.stdout.strip() == "65534"
    assert kill.exit_code != 0 and still.exit_code == 0  # the executor survived
