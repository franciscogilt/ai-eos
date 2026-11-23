"""The push path end to end with real Docker sandboxes and an in-memory GitHub.

A developer sandbox is seeded with the repository snapshot, changed, and its patch exported; the
gateway then pushes that patch artifact. Needs Docker (see test_docker_sandbox.py).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from typing import Any

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


# ------------------------------------------------------------------ B15: stacked tasks, review sandbox, scans
class MemoryStore:
    """Artifact store stand-in for the sandbox tools (patches and reports)."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, tuple[Any, bytes]] = {}

    async def put(self, org_id, data: bytes, *, kind: str, content_type: str, workflow_id=None):
        import hashlib
        from types import SimpleNamespace

        row = SimpleNamespace(
            id=uuid.uuid4(),
            kind=kind,
            workflow_id=workflow_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        self.items[row.id] = (row, data)
        return row

    async def get(self, org_id, artifact_id):
        return self.items[artifact_id]


def tool_ctx(wf: uuid.UUID, tool: str = "sandbox.prepare"):
    from aieos.gateway.catalog import ToolSpec
    from aieos.gateway.handlers import ToolContext

    spec = ToolSpec.model_validate(
        {"name": tool, "level": 2, "description": "d", "executes_in": "sandbox", "idempotent": True}
    )
    return ToolContext(uuid.uuid4(), wf, uuid.uuid4(), "developer", spec)


def test_tasks_stack_and_push_as_one_commit(setup):
    from aieos.gateway.sandbox_handlers import sandbox_handlers

    fake, base, wf, m = setup
    store = MemoryStore()
    snapshot = tarball(BASE)

    async def seed(_ctx) -> bytes:
        return snapshot

    tools = sandbox_handlers(m, store, snapshot=seed)  # type: ignore[arg-type]

    async def scenario() -> tuple[str, str]:
        # Task 1 in its own sandbox.
        t1 = tool_ctx(wf)
        await tools["sandbox.prepare"](t1, {"base_patches": [], "apply_patches": []})
        await tools["sandbox.write_file"](t1, {"path": "src/csv.py", "content": "def to_csv(rows):\n    return ''\n"})
        p1 = await tools["sandbox.export_patch"](t1, {"summary": "t1"})
        # Task 2 starts from base + approved task 1, and builds on its file.
        t2 = tool_ctx(wf)
        prepared = await tools["sandbox.prepare"](t2, {"base_patches": [p1["artifact_id"]], "apply_patches": []})
        assert prepared["base_patches"] == [p1["artifact_id"]]
        seen = await tools["sandbox.run_command"](t2, {"command": "cat src/csv.py"})
        assert "to_csv" in seen["stdout"]
        await tools["sandbox.write_file"](
            t2, {"path": "src/csv.py", "content": "def to_csv(rows):\n    return '\\n'.join(rows)\n"}
        )
        await tools["sandbox.write_file"](t2, {"path": "tests/test_csv.py", "content": "def test_x():\n    pass\n"})
        p2 = await tools["sandbox.export_patch"](t2, {"summary": "t2"})
        assert p2["files_changed"] == ["src/csv.py", "tests/test_csv.py"]  # only task 2's delta
        return p1["artifact_id"], p2["artifact_id"]

    p1, p2 = asyncio.run(scenario())

    class StackStore:
        async def get(self, org_id, artifact_id):
            row, data = store.items[artifact_id]
            row.workflow_id = wf
            return row, data

    gh = StubTools(fake, base, store=StackStore(), sandboxes=m)
    result = asyncio.run(
        gh.push_branch(ctx("github.push_branch", wf), {"patch_artifact_ids": [p1, p2], "branch": f"aieos/{wf}"})
    )
    files = fake.files_at(f"aieos/{wf}")
    assert files["src/csv.py"][1] == b"def to_csv(rows):\n    return '\\n'.join(rows)\n"
    assert "tests/test_csv.py" in files and files["README.md"] == BASE["README.md"]
    assert sorted((f["path"], f["change"]) for f in result["files_changed"]) == [
        ("src/csv.py", "add"),
        ("tests/test_csv.py", "add"),
    ]
    with pytest.raises(ToolInputError, match="does not apply"):  # out of order: task 2 needs task 1
        asyncio.run(
            gh.push_branch(ctx("github.push_branch", wf), {"patch_artifact_ids": [p2], "branch": f"aieos/{wf}"})
        )


def test_review_sandbox_and_security_scan(setup):
    from aieos.gateway.sandbox_handlers import sandbox_handlers

    _, _, wf, m = setup
    store = MemoryStore()

    async def seed(_ctx) -> bytes:
        return tarball(BASE)

    tools = sandbox_handlers(m, store, snapshot=seed)  # type: ignore[arg-type]
    fake_key = "AKIA" + "Q" * 16  # built at runtime so secret scanners on this repository stay quiet

    async def scenario():
        dev = tool_ctx(wf)
        await tools["sandbox.prepare"](dev, {"base_patches": [], "apply_patches": []})
        risky = f"import subprocess\nKEY = '{fake_key}'\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n"
        await tools["sandbox.write_file"](dev, {"path": "src/risky.py", "content": risky})
        patch = await tools["sandbox.export_patch"](dev, {"summary": "risky"})
        assert patch["policy_violations"] == []
        # The reviewer's sandbox: the patch applied, uncommitted, on a fresh copy.
        qa = tool_ctx(wf)
        await tools["sandbox.prepare"](qa, {"base_patches": [], "apply_patches": [patch["artifact_id"]]})
        diff = await tools["sandbox.run_command"](qa, {"command": "git status --porcelain"})
        assert "src/risky.py" in diff["stdout"]
        return await tools["sandbox.run_security_scan"](qa, {"scanners": None})

    scan = asyncio.run(scenario())
    rules = {(f["tool"], f["rule"].split()[0]) for f in scan["findings"]}
    assert ("secrets", "aws-access-key-id") in rules
    assert any(tool == "bandit" and rule == "B602" for tool, rule in rules)  # shell=True
    assert fake_key not in str(scan)  # the matched secret value is never echoed back
    assert scan["report_sha256"] and "bandit" in scan["scanners"]


def test_prepare_refuses_foreign_or_broken_patches(setup):
    from aieos.gateway.sandbox_handlers import sandbox_handlers

    _, _, wf, m = setup
    store = MemoryStore()
    foreign = asyncio.run(store.put(None, b"diff", kind="patch", content_type="x", workflow_id=uuid.uuid4()))
    broken = asyncio.run(
        store.put(
            None,
            b"diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-a\n+b\n",
            kind="patch",
            content_type="x",
            workflow_id=wf,
        )
    )

    async def seed(_ctx) -> bytes:
        return tarball(BASE)

    tools = sandbox_handlers(m, store, snapshot=seed)  # type: ignore[arg-type]
    with pytest.raises(ToolInputError, match="not a patch artifact of this workflow"):
        asyncio.run(tools["sandbox.prepare"](tool_ctx(wf), {"base_patches": [str(foreign.id)]}))
    with pytest.raises(ToolInputError, match="does not apply"):
        asyncio.run(tools["sandbox.prepare"](tool_ctx(wf), {"apply_patches": [str(broken.id)]}))
