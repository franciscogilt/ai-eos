"""Live check against the real GitHub test repository (CI only).

Uses the test GitHub App installed on the playground repository only. Configured through:
  AIEOS_TEST_GITHUB_APP_ID, AIEOS_TEST_GITHUB_INSTALLATION_ID, AIEOS_TEST_REPO (owner/name),
  AIEOS_TEST_GITHUB_APP_PRIVATE_KEY (PEM, from an Actions secret).
Skips when they are missing, unless AIEOS_REQUIRE_GITHUB=1 (set in CI for same-repository runs).
The branch it creates is deleted at the end; the default branch is only read.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from aieos.gateway.github_handlers import GitHubTools, WorkflowRepo
from aieos.integrations.github.app import GitHubApp, RepoRef
from aieos.sandbox.docker import SandboxKey
from tests.sandbox.test_docker_sandbox import _require_docker  # noqa: F401 - autouse fixture
from tests.sandbox.test_github_push import manager
from tests.unit.test_github import FakeStore, ctx

pytestmark = pytest.mark.docker

ENV = (
    "AIEOS_TEST_GITHUB_APP_ID",
    "AIEOS_TEST_GITHUB_INSTALLATION_ID",
    "AIEOS_TEST_REPO",
    "AIEOS_TEST_GITHUB_APP_PRIVATE_KEY",
)


@pytest.fixture(scope="module")
def live() -> tuple[GitHubApp, RepoRef]:
    missing = [v for v in ENV if not os.environ.get(v)]
    if missing:
        reason = f"live GitHub test not configured: {', '.join(missing)}"
        if os.environ.get("AIEOS_REQUIRE_GITHUB") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    owner, _, name = os.environ["AIEOS_TEST_REPO"].partition("/")
    app = GitHubApp(os.environ["AIEOS_TEST_GITHUB_APP_ID"], os.environ["AIEOS_TEST_GITHUB_APP_PRIVATE_KEY"])
    return app, RepoRef(owner=owner, name=name, installation_id=int(os.environ["AIEOS_TEST_GITHUB_INSTALLATION_ID"]))


class LiveTools(GitHubTools):
    def __init__(self, app: GitHubApp, wr: WorkflowRepo, store: FakeStore, sandboxes) -> None:
        super().__init__(db=None, app=app, store=store, sandboxes=sandboxes)  # type: ignore[arg-type]
        self._wr = wr

    async def workflow_repo(self, org_id, workflow_id):
        return self._wr


def test_snapshot_patch_and_push_against_real_github(live):
    app, ref = live
    repo = app.repo(ref)
    wf = uuid.uuid4()
    branch = f"aieos/{wf}/ci"
    path = f"aieos-ci/{wf}.txt"
    m = manager()

    async def scenario() -> None:
        base = await repo.branch_head("main")
        wr = WorkflowRepo(ref, "main", base, "AI-EOS CI: push path check")
        store = FakeStore("patch", wf, b"")
        tools = LiveTools(app, wr, store, m)

        readme = await tools.read_file(ctx("repo.read_file", wf), {"path": "README.md"})
        assert readme["size"] > 0

        key = SandboxKey(org_id=uuid.uuid4(), workflow_id=wf, agent_run_id=uuid.uuid4())

        async def seed() -> bytes:
            return await tools.snapshot_for(ctx("sandbox.write_file", wf))

        await m.ensure(key, snapshot=seed)
        await m.write_file(key, path, f"pushed by CI for workflow {wf}\n".encode())
        store.data, _ = await m.export_patch(key)
        try:
            result = await tools.push_branch(
                ctx("github.push_branch", wf), {"patch_artifact_id": str(uuid.uuid4()), "branch": branch}
            )
            assert result["base_commit_sha"] == base
            assert await repo.file_text(branch, path) == f"pushed by CI for workflow {wf}\n"
            assert await repo.file_text("main", path) is None  # default branch untouched
            assert await repo.branch_head("main") == base
        finally:
            try:
                await repo.delete_branch(branch)
            finally:
                await m.destroy_workflow(wf)
                await app.aclose()

    asyncio.run(scenario())
