"""Real-Docker checks that the sandbox isolates what it claims to.

Needs Docker and the sandbox image (built from infrastructure/docker/sandbox.Dockerfile). Without
Docker these skip, unless AIEOS_REQUIRE_DOCKER=1 (set in CI), in which case they fail.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator

import pytest

from aieos.sandbox.docker import DockerCLI, DockerSandboxManager, SandboxKey, SandboxLimits

pytestmark = pytest.mark.docker

IMAGE = os.environ.get("AIEOS_SANDBOX_IMAGE", "aieos-sandbox:local")


def _docker_ready() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    probe = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    if probe.returncode != 0:
        return f"docker daemon or image {IMAGE} unavailable"
    return None


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    reason = _docker_ready()
    if reason:
        if os.environ.get("AIEOS_REQUIRE_DOCKER") == "1":
            pytest.fail(reason)
        pytest.skip(reason)


@pytest.fixture
def sandbox() -> Iterator[tuple[DockerSandboxManager, SandboxKey]]:
    limits = SandboxLimits(cpus=1.0, memory_mb=256, pids=64, workspace_mb=16, tmp_mb=16, command_timeout_seconds=30)
    manager = DockerSandboxManager(DockerCLI(), image=IMAGE, limits=limits)
    key = SandboxKey(org_id=uuid.uuid4(), workflow_id=uuid.uuid4(), agent_run_id=uuid.uuid4())
    asyncio.run(manager.ensure(key, seed={"app.py": b"def add(a, b):\n    return a + b\n"}))
    yield manager, key
    asyncio.run(manager.destroy_workflow(key.workflow_id))


def run(manager: DockerSandboxManager, key: SandboxKey, *argv: str, timeout: int | None = None):
    return asyncio.run(manager.exec(key, list(argv), timeout=timeout))


def test_runs_unprivileged_without_capabilities(sandbox):
    manager, key = sandbox
    assert run(manager, key, "id", "-u").stdout.strip() == "65534"
    cap_eff = run(manager, key, "grep", "CapEff", "/proc/self/status").stdout.split()[-1]
    assert int(cap_eff, 16) == 0


def test_root_filesystem_is_read_only(sandbox):
    manager, key = sandbox
    r = run(manager, key, "touch", "/etc/aieos-was-here")
    assert r.exit_code != 0 and "Read-only file system" in r.stderr


def test_no_network(sandbox):
    manager, key = sandbox
    code = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(3)\n"
        "try:\n    s.connect(('1.1.1.1', 443)); print('CONNECTED')\n"
        "except OSError as e:\n    print('BLOCKED', e.errno)\n"
    )
    r = run(manager, key, "python", "-c", code)
    assert "BLOCKED" in r.stdout and "CONNECTED" not in r.stdout
    interfaces = run(manager, key, "cat", "/proc/net/dev").stdout
    assert "eth0" not in interfaces  # only loopback exists


def test_memory_limit_kills_the_process(sandbox):
    manager, key = sandbox
    r = run(manager, key, "python", "-c", "x = bytearray(600 * 1024 * 1024); print(len(x))")
    assert r.exit_code == 137 and "629145600" not in r.stdout


def test_time_limit(sandbox):
    manager, key = sandbox
    r = run(manager, key, "python", "-c", "import time; time.sleep(30)", timeout=2)
    assert r.exit_code == 137 and r.timed_out and r.duration_ms < 15_000


def test_process_limit(sandbox):
    manager, key = sandbox
    code = (
        "import subprocess\nprocs = []\n"
        "try:\n    for _ in range(200):\n        procs.append(subprocess.Popen(['sleep', '5']))\n"
        "    print('SPAWNED', len(procs))\n"
        "except OSError:\n    print('LIMITED', len(procs))\n"
        "finally:\n    [p.kill() for p in procs]\n"
    )
    r = run(manager, key, "python", "-c", code)
    assert "LIMITED" in r.stdout


def test_workspace_size_limit(sandbox):
    manager, key = sandbox
    r = run(manager, key, "python", "-c", "open('big.bin', 'wb').write(b'0' * 64 * 1024 * 1024)")
    assert r.exit_code != 0 and "No space left on device" in r.stderr


def test_edit_test_and_export_patch(sandbox):
    manager, key = sandbox
    test_file = b"from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    asyncio.run(manager.write_file(key, "test_app.py", test_file))
    assert run(manager, key, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider").exit_code == 0
    asyncio.run(manager.write_file(key, "app.py", b"def add(a, b):\n    return a + b + 0\n"))
    patch, files = asyncio.run(manager.export_patch(key))
    assert sorted(files) == ["app.py", "test_app.py"]
    assert b"+    return a + b + 0" in patch and b"+def test_add():" in patch


def test_patch_export_ignores_repository_git_tricks(sandbox):
    """Code inside the sandbox can reconfigure git; the exported patch must still show the real change."""
    manager, key = sandbox
    run(manager, key, "git", "config", "diff.external", "true")
    asyncio.run(manager.write_file(key, ".gitattributes", b"*.py -diff\n"))
    asyncio.run(manager.write_file(key, "app.py", b"def add(a, b):\n    return a - b  # sneaky\n"))
    patch, _ = asyncio.run(manager.export_patch(key))
    assert b"return a - b  # sneaky" in patch


def test_cleanup_removes_every_sandbox_of_the_workflow(sandbox):
    manager, key = sandbox
    sibling = SandboxKey(org_id=key.org_id, workflow_id=key.workflow_id, agent_run_id=uuid.uuid4())
    asyncio.run(manager.ensure(sibling))
    assert asyncio.run(manager.destroy_workflow(key.workflow_id)) == 2
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=aieos.workflow_id={key.workflow_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert listed.stdout.strip() == ""
