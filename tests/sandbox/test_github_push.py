"""The push path end to end with real Docker sandboxes and an in-memory GitHub.

A developer sandbox is seeded with the repository snapshot, changed, and its patch exported; the
gateway then pushes that patch artifact. Needs Docker (see test_docker_sandbox.py).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from aieos.gateway.handlers import ToolFailedError, ToolInputError
from aieos.sandbox.docker import DockerCLI, DockerSandboxManager, SandboxKey, SandboxLimits
from tests.github_fake import FakeGitHub, tarball
from tests.sandbox.test_docker_sandbox import IMAGE, _require_docker  # noqa: F401 - autouse fixture
from tests.unit.test_github import FakeStore, StubTools, ctx

pytestmark = pytest.mark.docker

BASE = {
    "README.md": ("100644", b"# app\n"),
    "src/app.py": ("100644", b"def add(a, b):\n    return a + b\n"),
    "old.txt": ("100644", b"bye\n"),
    "run.sh": ("100644", b"echo hi\n"),
    ".gitattributes": ("100644", b"*.py -diff\n"),  # must not hide changes from the exported patch
}


def manager() -> DockerSandboxManager:
    limits = SandboxLimits(cpus=1.0, memory_mb=256, pids=64, workspace_mb=32, tmp_mb=16, command_timeout_seconds=60)
    return DockerSandboxManager(DockerCLI(), image=IMAGE, limits=limits)


def developer_patch(m: DockerSandboxManager, snapshot: bytes, workflow_id: uuid.UUID) -> bytes:
    key = SandboxKey(org_id=uuid.uuid4(), workflow_id=workflow_id, agent_run_id=uuid.uuid4())

    async def seed() -> bytes:
        return snapshot

    async def work() -> bytes:
        await m.ensure(key, snapshot=seed)
        await m.write_file(key, "src/app.py", b"def add(a, b):\n    return a + b  # checked\n")
        await m.write_file(
            key, "tests/test_app.py", b"from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        await m.exec(key, ["rm", "old.txt"])
        await m.exec(key, ["chmod", "+x", "run.sh"])
        patch, _ = await m.export_patch(key)
        return patch

    try:
        return asyncio.run(work())
    finally:
        asyncio.run(m.destroy(key))


def containers(workflow_id: uuid.UUID) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=aieos.workflow_id={workflow_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


@pytest.fixture
def setup():
    fake = FakeGitHub()
    base = fake.commit(BASE)
    fake.refs["main"] = base
    wf = uuid.uuid4()
    m = manager()
    yield fake, base, wf, m
    asyncio.run(m.destroy_workflow(wf))


def push(tools: StubTools, wf: uuid.UUID, branch: str | None = None):
    return asyncio.run(
        tools.push_branch(
            ctx("github.push_branch", wf),
            {"patch_artifact_id": str(uuid.uuid4()), "branch": branch or f"aieos/{wf}/T-1"},
        )
    )


def test_reviewed_patch_is_pushed_as_one_commit_on_the_base(setup):
    fake, base, wf, m = setup
    patch = developer_patch(m, tarball(BASE), wf)
    tools = StubTools(fake, base, store=FakeStore("patch", wf, patch), sandboxes=m)

    result = push(tools, wf)

    branch = f"aieos/{wf}/T-1"
    assert result["branch"] == branch and result["base_commit_sha"] == base
    assert sorted((f["path"], f["change"]) for f in result["files_changed"]) == [
        ("old.txt", "delete"),
        ("run.sh", "modify"),
        ("src/app.py", "modify"),
        ("tests/test_app.py", "add"),
    ]
    files = fake.files_at(branch)
    assert files["src/app.py"][1].endswith(b"# checked\n")
    assert files["tests/test_app.py"][0] == "100644" and "old.txt" not in files
    assert files["run.sh"] == ("100755", b"echo hi\n")
    assert files["README.md"] == BASE["README.md"] and files[".gitattributes"] == BASE[".gitattributes"]
    assert fake.commits[result["commit_sha"]]["parents"] == [base]
    assert "main" in fake.refs and fake.refs["main"] == base  # the default branch is never touched
    assert containers(wf) == []  # the push sandbox is gone

    again = push(tools, wf)  # idempotent: re-running moves the branch to an equivalent commit
    assert fake.files_at(branch) == files and again["base_commit_sha"] == base


def test_patch_that_does_not_apply_is_rejected(setup):
    fake, base, wf, m = setup
    stale = b"""diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a * b
"""
    tools = StubTools(fake, base, store=FakeStore("patch", wf, stale), sandboxes=m)
    with pytest.raises(ToolInputError, match="does not apply"):
        push(tools, wf)
    assert set(fake.refs) == {"main"} and containers(wf) == []


def test_snapshot_that_differs_from_github_is_refused(setup):
    fake, base, wf, m = setup
    patch = developer_patch(m, tarball(BASE), wf)
    tampered = dict(BASE)
    tampered["src/app.py"] = ("100644", b"def add(a, b):\n    return a + b\n# injected\n")
    fake.tarball_override = tarball(tampered)
    tools = StubTools(fake, base, store=FakeStore("patch", wf, patch), sandboxes=m)
    with pytest.raises((ToolFailedError, ToolInputError)):
        push(tools, wf)
    assert set(fake.refs) == {"main"}


def test_adding_a_file_that_already_exists_is_refused(setup):
    fake, base, wf, m = setup
    clash = b"""diff --git a/README.md b/README.md
new file mode 100644
--- /dev/null
+++ b/README.md
@@ -0,0 +1 @@
+# replaced
"""
    tools = StubTools(fake, base, store=FakeStore("patch", wf, clash), sandboxes=m)
    with pytest.raises(ToolInputError):
        push(tools, wf)
    assert set(fake.refs) == {"main"}
