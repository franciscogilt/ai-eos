"""Sandboxes as ECS Fargate tasks (AWS).

Each sandbox is its own Fargate task, a separate micro-VM, so agent code never shares a kernel with
the tool gateway or with another sandbox:
* task subnets have no route to the internet; its security group admits only the gateway on the
  executor port and allows outbound traffic only to the VPC's AWS endpoints (image pull);
* the task has no IAM task role, so it holds no AWS credentials;
* read-only root filesystem; /workspace and /tmp are task-local volumes; CPU and memory per task;
* commands run through the in-sandbox executor (sandbox/executor.py) as uid 65534, never as root.

The gateway reaches a sandbox at its private IP with a per-sandbox token: HMAC-SHA256 of the sandbox
name under a gateway secret. It can therefore always re-derive the token after a restart and resume a
step in the same sandbox, but nobody can guess the token for a sandbox from its name.
Tasks are tagged with org, workflow, agent run and creation time, and started with
`startedBy=<sandbox name>`, so they can be found and stopped without any local state.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Protocol
from uuid import UUID

import httpx

from aieos.core.logging import get_logger
from aieos.sandbox.base import WORKSPACE, Completed, SandboxError, SandboxKey, SandboxLimits, SandboxManager
from aieos.sandbox.executor import frame, unframe

log = get_logger(__name__)

CONTAINER = "sandbox"
EXECUTOR_PORT = 7070


class EcsClient(Protocol):
    """The boto3 ECS client methods used here (replaceable in tests)."""

    def run_task(self, **kwargs: Any) -> dict[str, Any]: ...
    def stop_task(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_tasks(self, **kwargs: Any) -> dict[str, Any]: ...
    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]: ...


def _tags(task: dict[str, Any]) -> dict[str, str]:
    return {t["key"]: t["value"] for t in task.get("tags", [])}


def _private_ip(task: dict[str, Any]) -> str | None:
    for attachment in task.get("attachments", []):
        for detail in attachment.get("details", []):
            if detail.get("name") == "privateIPv4Address":
                return str(detail["value"])
    return None


class FargateSandboxManager(SandboxManager):
    def __init__(
        self,
        ecs: EcsClient,
        *,
        cluster: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        token_secret: bytes,
        limits: SandboxLimits | None = None,
        port: int = EXECUTOR_PORT,
        startup_timeout_seconds: float = 300.0,
        poll_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if len(token_secret) < 32:
            raise ValueError("sandbox token secret must be at least 32 bytes")
        self._ecs = ecs
        self._cluster = cluster
        self._task_definition = task_definition
        self._subnets = subnets
        self._security_groups = security_groups
        self._secret = token_secret
        self._limits = limits or SandboxLimits()
        self._port = port
        self._startup = startup_timeout_seconds
        self._poll = poll_seconds
        self._http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60.0, connect=10.0))
        self._ready: set[str] = set()
        self._addresses: dict[str, str] = {}

    def token(self, key: SandboxKey) -> str:
        return hmac.new(self._secret, key.name.encode(), hashlib.sha256).hexdigest()

    async def _ecs_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(getattr(self._ecs, method), **kwargs)

    async def _tasks(self, *, started_by: str | None = None) -> list[dict[str, Any]]:
        arns: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"cluster": self._cluster, "desiredStatus": "RUNNING"}
            if started_by:
                params["startedBy"] = started_by
            else:
                params["family"] = self._task_definition.split("/")[-1].split(":")[0]
            if token:
                params["nextToken"] = token
            page = await self._ecs_call("list_tasks", **params)
            arns += page.get("taskArns", [])
            token = page.get("nextToken")
            if not token:
                break
        tasks: list[dict[str, Any]] = []
        for i in range(0, len(arns), 100):
            described = await self._ecs_call(
                "describe_tasks", cluster=self._cluster, tasks=arns[i : i + 100], include=["TAGS"]
            )
            tasks += described.get("tasks", [])
        return [t for t in tasks if _tags(t).get("aieos.sandbox") == "1"]

    async def _find(self, key: SandboxKey) -> dict[str, Any] | None:
        tasks = await self._tasks(started_by=key.name)
        return tasks[0] if tasks else None

    # ------------------------------------------------------------------ lifecycle
    async def _create(self, key: SandboxKey) -> None:
        lim = self._limits
        response = await self._ecs_call(
            "run_task",
            cluster=self._cluster,
            taskDefinition=self._task_definition,
            launchType="FARGATE",
            count=1,
            startedBy=key.name,
            enableExecuteCommand=False,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {"name": CONTAINER, "environment": [{"name": "AIEOS_EXECUTOR_TOKEN", "value": self.token(key)}]}
                ],
                "cpu": str(int(lim.cpus * 1024)),
                "memory": str(lim.memory_mb),
            },
            tags=[
                {"key": "aieos.sandbox", "value": "1"},
                {"key": "aieos.org_id", "value": str(key.org_id)},
                {"key": "aieos.workflow_id", "value": str(key.workflow_id)},
                {"key": "aieos.agent_run_id", "value": str(key.agent_run_id)},
                {"key": "aieos.created_at", "value": str(int(time.time()))},
            ],
        )
        if response.get("failures") or not response.get("tasks"):
            raise SandboxError(f"could not start sandbox task: {response.get('failures')}")
        arn = response["tasks"][0]["taskArn"]
        deadline = time.monotonic() + self._startup
        while time.monotonic() < deadline:
            described = await self._ecs_call("describe_tasks", cluster=self._cluster, tasks=[arn], include=["TAGS"])
            task = (described.get("tasks") or [{}])[0]
            if task.get("lastStatus") == "STOPPED":
                raise SandboxError(f"sandbox task stopped while starting: {task.get('stoppedReason')}")
            address = _private_ip(task)
            if task.get("lastStatus") == "RUNNING" and address and await self._healthy(address):
                self._addresses[key.name] = address
                log.info("sandbox.task_started", sandbox=key.name, task=arn)
                return
            await asyncio.sleep(self._poll)
        await self._stop([arn], "start timeout")
        raise SandboxError(f"sandbox task did not become ready within {self._startup:.0f}s")

    async def _healthy(self, address: str) -> bool:
        try:
            r = await self._http.get(f"http://{address}:{self._port}/healthz", timeout=5.0)
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def _exists(self, key: SandboxKey) -> bool:
        task = await self._find(key)
        if task is None or task.get("lastStatus") != "RUNNING":
            return False
        address = _private_ip(task)
        if address:
            self._addresses[key.name] = address
        return address is not None

    async def _stop(self, arns: list[str], reason: str) -> None:
        for arn in arns:
            await self._ecs_call("stop_task", cluster=self._cluster, task=arn, reason=f"aieos: {reason}"[:255])

    async def destroy(self, key: SandboxKey) -> None:
        tasks = await self._tasks(started_by=key.name)
        await self._stop([t["taskArn"] for t in tasks], "sandbox destroyed")
        self._ready.discard(key.name)
        self._addresses.pop(key.name, None)

    async def destroy_workflow(self, workflow_id: UUID) -> int:
        tasks = [t for t in await self._tasks() if _tags(t).get("aieos.workflow_id") == str(workflow_id)]
        await self._stop([t["taskArn"] for t in tasks], "workflow finished")
        names = {str(t.get("startedBy")) for t in tasks}
        self._ready -= names
        for name in names:
            self._addresses.pop(name, None)
        log.info("sandbox.workflow_cleaned", workflow_id=str(workflow_id), removed=len(tasks))
        return len(tasks)

    async def reap(self, *, older_than_seconds: int) -> int:
        cutoff = time.time() - older_than_seconds
        stale = [
            t["taskArn"]
            for t in await self._tasks()
            if (ts := _tags(t).get("aieos.created_at", "")).isdigit() and int(ts) < cutoff
        ]
        await self._stop(stale, "older than the sandbox age limit")
        return len(stale)

    # ------------------------------------------------------------------ commands
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
        address = self._addresses.get(key.name)
        if address is None:
            if not await self._exists(key):
                raise SandboxError(f"sandbox {key.name} is not running")
            address = self._addresses[key.name]
        if workdir != WORKSPACE and not workdir.startswith(WORKSPACE + "/"):
            raise SandboxError("workdir must be inside the workspace")
        relative = workdir[len(WORKSPACE) :].lstrip("/")  # the executor resolves it under its workspace
        body = frame({"argv": argv, "timeout": seconds, "workdir": relative}, stdin or b"")
        try:
            r = await self._http.post(
                f"http://{address}:{self._port}/exec",
                content=body,
                headers={"Authorization": f"Bearer {self.token(key)}", "Content-Type": "application/octet-stream"},
                timeout=seconds + 30,
            )
        except httpx.TimeoutException:
            return Completed(-1, b"", b"sandbox executor did not answer in time", timed_out=True)
        except httpx.HTTPError as exc:
            self._addresses.pop(key.name, None)
            raise SandboxError(f"sandbox executor unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise SandboxError(f"sandbox executor refused the command: HTTP {r.status_code}: {r.text[:300]}")
        header, payload = unframe(r.content)
        n_out = int(header["stdout_len"])
        return Completed(int(header["exit_code"]), payload[:n_out], payload[n_out:], bool(header["timed_out"]))

    async def aclose(self) -> None:
        await self._http.aclose()
