"""A throwaway database with the schema applied and a runtime login role, for API tests.

Needs ADMIN_DATABASE_URL: a role that can CREATE DATABASE and CREATE ROLE (non-superuser
is fine and preferred). Everything created here is dropped at the end of the session.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

ADMIN_URL = os.environ.get("ADMIN_DATABASE_URL")
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations" / "sql").glob("*.sql"))


@dataclass(frozen=True)
class ProvisionedDb:
    owner_conninfo: str  # libpq, table owner
    owner_async_url: str  # SQLAlchemy, table owner (used to prove the API refuses it)
    app_async_url: str  # SQLAlchemy, runtime role (member of aieos_app)
    app_password: str


def _sqlalchemy_url(conninfo: str) -> str:
    p = psycopg.conninfo.conninfo_to_dict(conninfo)
    return f"postgresql+psycopg://{p['user']}:{p['password']}@{p['host']}:{p['port']}/{p['dbname']}"


@pytest.fixture(scope="session")
def provisioned_db() -> Iterator[ProvisionedDb]:
    if not ADMIN_URL:
        pytest.skip("ADMIN_DATABASE_URL not set")
    suffix = uuid.uuid4().hex[:8]
    name, role, password = f"aieos_it_{suffix}", f"aieos_it_api_{suffix}", uuid.uuid4().hex
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    owner = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    with psycopg.connect(owner, autocommit=True) as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text())
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE aieos_app")
    app = psycopg.conninfo.make_conninfo(owner, user=role, password=password)
    try:
        yield ProvisionedDb(owner, _sqlalchemy_url(owner), _sqlalchemy_url(app), password)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            admin.execute(f"DROP ROLE IF EXISTS {role}")


ENGINEERING = Path(__file__).resolve().parents[2] / "engineering"


@pytest.fixture(scope="session")
def published_agents(provisioned_db: ProvisionedDb) -> list[str]:
    from aieos.cli import admin

    return admin.publish_agents(provisioned_db.owner_conninfo, ENGINEERING)


def seed_workflow(
    owner_conninfo: str,
    org_id: uuid.UUID,
    *,
    budget_usd: str = "25",
    budget_tokens: int = 3_000_000,
    agent_key: str = "developer",
) -> dict[str, uuid.UUID]:
    """Project + repository + workflow + agent run, as the owner. Requires published agents."""
    from aieos.core.ids import uuid7

    ids = {k: uuid7() for k in ("project", "repo", "workflow", "run")}
    with psycopg.connect(owner_conninfo) as c:
        user = c.execute(
            "SELECT user_id FROM memberships WHERE org_id = %s AND role = 'owner' LIMIT 1", (org_id,)
        ).fetchone()[0]
        version = c.execute(
            """SELECT v.id FROM agent_versions v JOIN agents a ON a.id = v.agent_id
                               WHERE a.org_id IS NULL AND a.key = %s""",
            (agent_key,),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO projects (id, org_id, slug, name) VALUES (%s, %s, %s, 'P')",
            (ids["project"], org_id, f"p-{ids['project'].hex[-12:]}"),
        )
        c.execute(
            """INSERT INTO repositories (id, org_id, project_id, external_id, owner_login, name,
                     github_installation_id) VALUES (%s, %s, %s, %s, 'acme', 'app', 1)""",
            (ids["repo"], org_id, ids["project"], ids["repo"].int % 10**12),
        )
        c.execute(
            """INSERT INTO workflows (id, org_id, project_id, repository_id, kind, title, temporal_workflow_id,
                     idempotency_key, created_by, budget_usd, budget_tokens)
                     VALUES (%s, %s, %s, %s, 'feature_delivery', 't', %s, %s, %s, %s, %s)""",
            (
                ids["workflow"],
                org_id,
                ids["project"],
                ids["repo"],
                f"wf-{ids['workflow']}",
                str(ids["workflow"]),
                user,
                budget_usd,
                budget_tokens,
            ),
        )
        c.execute(
            """INSERT INTO agent_runs (id, org_id, workflow_id, agent_version_id, model_provider, model_name)
                     VALUES (%s, %s, %s, %s, 'openai', 'model-a')""",
            (ids["run"], org_id, ids["workflow"], version),
        )
    return ids
