"""Applies the initial migration to a fresh database and checks tenant isolation,
cross-tenant FK protection, audit immutability and the four-eyes constraint.

Requires ADMIN_DATABASE_URL pointing at a PostgreSQL 16+ server where the user can
CREATE DATABASE and CREATE ROLE. Skipped when unset.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

ADMIN_URL = os.environ.get("ADMIN_DATABASE_URL")
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations" / "sql").glob("*.sql"))

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ADMIN_DATABASE_URL not set")


@pytest.fixture(scope="module")
def db() -> Iterator[dict[str, str]]:
    name = f"aieos_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    owner_url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    with psycopg.connect(owner_url, autocommit=True) as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text())
        conn.execute("""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieos_api_test') THEN
                CREATE ROLE aieos_api_test LOGIN PASSWORD 'test-only' IN ROLE aieos_app;
              END IF;
            END $$;""")
    app_url = psycopg.conninfo.make_conninfo(owner_url, user="aieos_api_test", password="test-only")
    try:
        yield {"owner": owner_url, "app": app_url}
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def seed_tenant(owner_url: str, slug: str) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("org", "user", "approver", "project", "repo", "workflow")}
    with psycopg.connect(owner_url, autocommit=True) as c:
        c.execute("INSERT INTO organizations (id, slug, name) VALUES (%s, %s, %s)", (ids["org"], slug, slug))
        for key in ("user", "approver"):
            c.execute(
                "INSERT INTO users (id, idp_subject, email) VALUES (%s, %s, %s)",
                (ids[key], f"sub-{ids[key]}", f"{ids[key]}@example.test"),
            )
        c.execute(
            "INSERT INTO projects (id, org_id, slug, name) VALUES (%s, %s, 'core', 'Core')",
            (ids["project"], ids["org"]),
        )
        c.execute(
            """INSERT INTO repositories (id, org_id, project_id, external_id, owner_login, name,
                     github_installation_id) VALUES (%s, %s, %s, %s, 'acme', 'app', 1)""",
            (ids["repo"], ids["org"], ids["project"], abs(hash(slug)) % 10**9),
        )
        c.execute(
            """INSERT INTO workflows (id, org_id, project_id, repository_id, kind, title,
                     temporal_workflow_id, idempotency_key, created_by, budget_usd, budget_tokens)
                     VALUES (%s, %s, %s, %s, 'feature_delivery', 't', %s, 'k1', %s, 25, 3000000)""",
            (ids["workflow"], ids["org"], ids["project"], ids["repo"], f"wf-{ids['workflow']}", ids["user"]),
        )
    return ids


@pytest.fixture(scope="module")
def tenants(db):
    return seed_tenant(db["owner"], "acme"), seed_tenant(db["owner"], "globex")


def app_conn(db, org_id):
    conn = psycopg.connect(db["app"])
    conn.execute("SELECT set_config('app.org_id', %s, false)", (str(org_id),))
    return conn


def test_rls_hides_other_tenants(db, tenants):
    a, _ = tenants
    with app_conn(db, a["org"]) as c:
        rows = c.execute("SELECT id FROM workflows").fetchall()
        assert [r[0] for r in rows] == [a["workflow"]]
        assert c.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1


def test_no_tenant_context_sees_nothing(db, tenants):
    with psycopg.connect(db["app"]) as c:
        assert c.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0


def test_rls_blocks_writes_into_other_tenant(db, tenants):
    a, b = tenants
    with app_conn(db, a["org"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("INSERT INTO projects (org_id, slug, name) VALUES (%s, 'evil', 'x')", (b["org"],))


def test_composite_fk_blocks_cross_tenant_reference(db, tenants):
    a, b = tenants
    # Even the owner (bypassing RLS) cannot attach a workflow to another tenant's repository.
    with psycopg.connect(db["owner"]) as c, pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute(
            """INSERT INTO workflows (org_id, project_id, repository_id, kind, title,
                     temporal_workflow_id, idempotency_key, created_by, budget_usd, budget_tokens)
                     VALUES (%s, %s, %s, 'feature_delivery', 'x', 'wf-x', 'k2', %s, 1, 1)""",
            (a["org"], a["project"], b["repo"], a["user"]),
        )


def test_workflow_idempotency_key_unique_per_tenant(db, tenants):
    a, _ = tenants
    with psycopg.connect(db["owner"]) as c, pytest.raises(psycopg.errors.UniqueViolation):
        c.execute(
            """INSERT INTO workflows (org_id, project_id, repository_id, kind, title,
                     temporal_workflow_id, idempotency_key, created_by, budget_usd, budget_tokens)
                     VALUES (%s, %s, %s, 'feature_delivery', 'dup', 'wf-dup', 'k1', %s, 1, 1)""",
            (a["org"], a["project"], a["repo"], a["user"]),
        )


def insert_audit(c, org_id, action):
    return c.execute(
        """INSERT INTO audit_events (org_id, actor_type, actor_id, action, target_type, target_id)
                        VALUES (%s, 'system', 'test', %s, 'workflow', 'x')
                        RETURNING chain_seq, prev_hash, hash""",
        (org_id, action),
    ).fetchone()


def test_audit_chain_and_append_only(db, tenants):
    a, _ = tenants
    with app_conn(db, a["org"]) as c:
        s1, p1, h1 = insert_audit(c, a["org"], "one")
        s2, p2, h2 = insert_audit(c, a["org"], "two")
        c.commit()
        assert (s1, s2) == (1, 2) and p1 is None and p2 == h1 and h1 != h2
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE audit_events SET action = 'tampered'")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM audit_events")
    # Even the owner is blocked by the trigger.
    with psycopg.connect(db["owner"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM audit_events")


def test_audit_chains_are_per_tenant(db, tenants):
    _, b = tenants
    with app_conn(db, b["org"]) as c:
        seq, prev, _ = insert_audit(c, b["org"], "first-for-b")
        assert seq == 1 and prev is None


def test_four_eyes_constraint(db, tenants):
    a, _ = tenants
    with app_conn(db, a["org"]) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute(
            """INSERT INTO approvals (org_id, workflow_id, kind, permission_level, request_digest, summary,
                     status, requested_by_user_id, decided_by_user_id, decided_at, expires_at)
                     VALUES (%s, %s, 'production_deploy', 4, %s, 'deploy', 'approved', %s, %s, now(),
                     now() + interval '1 hour')""",
            (a["org"], a["workflow"], "0" * 64, a["user"], a["user"]),
        )


def test_waived_gate_requires_approval(db, tenants):
    a, _ = tenants
    with app_conn(db, a["org"]) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute(
            "INSERT INTO quality_gates (org_id, workflow_id, gate_key, result) VALUES (%s, %s, 'tests_pass', 'waived')",
            (a["org"], a["workflow"]),
        )


def test_audit_chain_survives_concurrent_writers(db, tenants):
    import itertools
    from concurrent.futures import ThreadPoolExecutor

    org = uuid.uuid4()
    with psycopg.connect(db["owner"], autocommit=True) as c:
        c.execute("INSERT INTO organizations (id, slug, name) VALUES (%s, 'concurrent', 'c')", (org,))

    def writer(i: int) -> None:
        with app_conn(db, org) as c:
            insert_audit(c, org, f"evt-{i}")
            c.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(40)))

    with app_conn(db, org) as c:
        rows = c.execute("SELECT chain_seq, prev_hash, hash FROM audit_events ORDER BY chain_seq").fetchall()
    assert [r[0] for r in rows] == list(range(1, 41))
    for prev, cur in itertools.pairwise(rows):
        assert cur[1] == prev[2], "chain forked"
