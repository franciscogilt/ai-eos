"""Workflow -> repository resolution and the pinned base commit (migration 0005), against PostgreSQL."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from aieos.cli import admin
from aieos.gateway.github_handlers import GitHubTools
from aieos.persistence.db import Database
from tests.github_fake import FakeGitHub
from tests.integration.conftest import seed_workflow
from tests.unit.test_github import app


@pytest.fixture(scope="module")
def org(provisioned_db, published_agents):
    return admin.create_org(
        provisioned_db.owner_conninfo, slug="gh-repo", name="G", owner_sub="gh-owner", owner_email="o@gh.test"
    )


def resolve(provisioned_db, org_id, wf, fake):
    async def main():
        db = Database(provisioned_db.app_async_url, pool_size=2, max_overflow=0)
        try:
            tools = GitHubTools(db, app(fake), store=None, sandboxes=None)  # type: ignore[arg-type]
            return await tools.workflow_repo(org_id, wf)
        finally:
            await db.dispose()

    return asyncio.run(main())


def test_base_commit_is_pinned_once(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    fake = FakeGitHub()
    first = fake.commit({"a.txt": ("100644", b"1")})
    fake.refs["main"] = first

    wr = resolve(provisioned_db, org, wf, fake)
    assert wr.base_sha == first and wr.repo.full_name == "acme/app" and wr.repo.installation_id == 1

    fake.refs["main"] = fake.commit({"a.txt": ("100644", b"2")}, parent=first, message="later")
    assert resolve(provisioned_db, org, wf, fake).base_sha == first  # main moved; the workflow did not

    with psycopg.connect(provisioned_db.owner_conninfo) as c, pytest.raises(psycopg.errors.RaiseException):
        c.execute("UPDATE workflows SET base_commit_sha = %s WHERE id = %s", ("0" * 40, wf))


def test_other_organizations_workflows_are_invisible(provisioned_db, org):
    other = admin.create_org(
        provisioned_db.owner_conninfo, slug="gh-other", name="O", owner_sub="gh-o2", owner_email="o2@gh.test"
    )
    wf = seed_workflow(provisioned_db.owner_conninfo, other)["workflow"]
    from aieos.gateway.handlers import ToolInputError

    with pytest.raises(ToolInputError, match="workflow not found"):
        resolve(provisioned_db, org, wf, FakeGitHub())
