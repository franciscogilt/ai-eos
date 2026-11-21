"""API behaviour against real PostgreSQL with RLS. Nothing here mocks the tenant filter."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aieos.api.app import create_app
from aieos.cli import admin
from aieos.core.settings import Settings
from aieos.persistence.db import RuntimeRoleError

ISS, AUD = "https://issuer.test", "aieos-api"
ROLES = ("admin", "approver", "member", "viewer")


def make_settings(db, jwks, **overrides) -> Settings:
    values = {
        "environment": "test",
        "database_url": db.app_async_url,
        "oidc_issuer": ISS,
        "oidc_audience": AUD,
        "oidc_jwks_path": jwks,
        "log_json": True,
        "orchestrator_mode": "disabled",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="module")
def env(provisioned_db, tmp_path_factory):
    keys = tmp_path_factory.mktemp("keys")
    jwks = admin.dev_keys(keys)
    owner = provisioned_db.owner_conninfo
    org_a = admin.create_org(owner, slug="acme", name="Acme", owner_sub="owner-a", owner_email="owner@acme.test")
    org_b = admin.create_org(owner, slug="globex", name="Globex", owner_sub="owner-b", owner_email="owner@globex.test")
    for role in ROLES:
        admin.add_member(owner, org_slug="acme", sub=f"{role}-a", email=f"{role}@acme.test", role=role)
    log = io.StringIO()
    tokens: list[str] = []

    def auth(sub: str, **kw) -> dict[str, str]:
        params = {"issuer": ISS, "audience": AUD, "email": f"{sub}@x.test", **kw}
        tok = admin.dev_token(keys, sub=sub, **params)
        tokens.append(tok)
        return {"Authorization": f"Bearer {tok}"}

    # Pool of exactly one connection: every request reuses it, which is the worst case for tenant leakage.
    app = create_app(make_settings(provisioned_db, jwks, db_pool_size=1, db_max_overflow=0), log_stream=log)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, app=app, a=org_a, b=org_b, auth=auth, log=log, tokens=tokens, db=provisioned_db, jwks=jwks
        )


def new_project(env, org, sub, slug):
    r = env.client.post(f"/v1/orgs/{org}/projects", json={"slug": slug, "name": slug.title()}, headers=env.auth(sub))
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ health and auth
def test_health_and_readiness(env):
    assert env.client.get("/healthz").json() == {"status": "ok"}
    assert env.client.get("/readyz").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic dXNlcjpwdw=="}, {"Authorization": "Bearer not.a.token"}],
)
def test_unauthenticated(env, headers):
    r = env.client.get("/v1/me", headers=headers)
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthenticated"


def test_expired_and_wrong_audience(env):
    assert env.client.get("/v1/me", headers=env.auth("owner-a", ttl_seconds=-120)).status_code == 401
    assert env.client.get("/v1/me", headers=env.auth("owner-a", audience="other")).status_code == 401


def test_me_creates_user_just_in_time(env):
    r = env.client.get("/v1/me", headers=env.auth("brand-new-user", email="new@x.test"))
    assert r.status_code == 200 and r.json()["subject"] == "brand-new-user"
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Request-ID"]


# ------------------------------------------------------------------ tenancy
def test_org_visible_to_members_only(env):
    r = env.client.get(f"/v1/orgs/{env.a}", headers=env.auth("viewer-a"))
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    # Non-member: indistinguishable from a non-existent org.
    assert env.client.get(f"/v1/orgs/{env.a}", headers=env.auth("owner-b")).status_code == 404
    assert (
        env.client.get("/v1/orgs/00000000-0000-7000-8000-000000000000", headers=env.auth("owner-a")).status_code == 404
    )


def test_cross_tenant_project_access_is_404(env):
    p = new_project(env, env.a, "owner-a", "secret-project")
    other = env.auth("owner-b")
    assert env.client.get(f"/v1/orgs/{env.a}/projects/{p['id']}", headers=other).status_code == 404
    # Valid membership in B + project id from A: still not found (RLS), not 403.
    assert env.client.get(f"/v1/orgs/{env.b}/projects/{p['id']}", headers=other).status_code == 404
    patch = {"version": p["version"], "name": "pwned"}
    assert env.client.patch(f"/v1/orgs/{env.b}/projects/{p['id']}", json=patch, headers=other).status_code == 404
    assert env.client.delete(f"/v1/orgs/{env.b}/projects/{p['id']}", headers=other).status_code == 404
    listed = env.client.get(f"/v1/orgs/{env.b}/projects", headers=other).json()
    assert p["id"] not in {x["id"] for x in listed}


def test_pooled_connection_does_not_leak_tenant(env):
    new_project(env, env.b, "owner-b", "globex-only")
    for _ in range(3):  # alternate tenants over the single pooled connection
        a = {x["slug"] for x in env.client.get(f"/v1/orgs/{env.a}/projects", headers=env.auth("owner-a")).json()}
        b = {x["slug"] for x in env.client.get(f"/v1/orgs/{env.b}/projects", headers=env.auth("owner-b")).json()}
        assert "globex-only" not in a and "globex-only" in b

    async def leftover_setting() -> str | None:
        async with env.app.state.db.engine.connect() as conn:
            return (await conn.execute(text("SELECT current_setting('app.org_id', true)"))).scalar()

    assert env.client.portal.call(leftover_setting) in (None, "")


# ------------------------------------------------------------------ RBAC
@pytest.mark.parametrize("role,expected", [("admin", 201), ("approver", 403), ("member", 403), ("viewer", 403)])
def test_project_create_by_role(env, role, expected):
    r = env.client.post(
        f"/v1/orgs/{env.a}/projects", json={"slug": f"by-{role}", "name": "x"}, headers=env.auth(f"{role}-a")
    )
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("role", ROLES)
def test_everyone_can_read(env, role):
    assert env.client.get(f"/v1/orgs/{env.a}/projects", headers=env.auth(f"{role}-a")).status_code == 200


# ------------------------------------------------------------------ projects
def test_duplicate_slug_conflict(env):
    new_project(env, env.a, "owner-a", "dup")
    r = env.client.post(
        f"/v1/orgs/{env.a}/projects", json={"slug": "dup", "name": "again"}, headers=env.auth("owner-a")
    )
    assert r.status_code == 409


def test_optimistic_concurrency(env):
    p = new_project(env, env.a, "owner-a", "occ")
    url, h = f"/v1/orgs/{env.a}/projects/{p['id']}", env.auth("admin-a")
    r1 = env.client.patch(url, json={"version": 1, "name": "First"}, headers=h)
    assert r1.status_code == 200 and r1.json()["version"] == 2
    r2 = env.client.patch(url, json={"version": 1, "name": "Stale write"}, headers=h)
    assert r2.status_code == 409 and "expected 1, current is 2" in r2.json()["error"]["message"]
    assert env.client.get(url, headers=h).json()["name"] == "First"


def test_policy_is_validated(env):
    good = {"slug": "pol", "name": "P", "policy": {"auto_approved_level3_tools": ["github.create_pull_request"]}}
    r = env.client.post(f"/v1/orgs/{env.a}/projects", json=good, headers=env.auth("owner-a"))
    assert r.status_code == 201 and r.json()["policy"]["auto_approved_level3_tools"] == ["github.create_pull_request"]
    bad = {"slug": "pol2", "name": "P", "policy": {"allow_everything": True}}
    assert env.client.post(f"/v1/orgs/{env.a}/projects", json=bad, headers=env.auth("owner-a")).status_code == 422


def test_unknown_fields_rejected(env):
    body = {"slug": "x-extra", "name": "x", "org_id": str(env.b)}
    assert env.client.post(f"/v1/orgs/{env.a}/projects", json=body, headers=env.auth("owner-a")).status_code == 422


def test_delete_project(env):
    p = new_project(env, env.a, "owner-a", "to-delete")
    url = f"/v1/orgs/{env.a}/projects/{p['id']}"
    assert env.client.delete(url, headers=env.auth("viewer-a")).status_code == 403
    assert env.client.delete(url, headers=env.auth("owner-a")).status_code == 204
    assert env.client.get(url, headers=env.auth("owner-a")).status_code == 404


# ------------------------------------------------------------------ repositories
def test_repository_registration(env):
    p = new_project(env, env.a, "owner-a", "with-repo")
    url = f"/v1/orgs/{env.a}/projects/{p['id']}/repositories"
    body = {"owner_login": "acme", "name": "web-app", "external_id": 123456, "github_installation_id": 42}
    assert env.client.post(url, json=body, headers=env.auth("member-a")).status_code == 403
    r = env.client.post(url, json=body, headers=env.auth("admin-a"))
    assert r.status_code == 201 and r.json()["full_name"] == "acme/web-app"
    assert env.client.post(url, json=body, headers=env.auth("admin-a")).status_code == 409
    assert [x["name"] for x in env.client.get(url, headers=env.auth("viewer-a")).json()] == ["web-app"]
    bad = {**body, "external_id": 7, "default_branch": "../../etc"}
    assert env.client.post(url, json=bad, headers=env.auth("admin-a")).status_code == 422


def test_repository_in_unknown_project_is_404(env):
    url = f"/v1/orgs/{env.a}/projects/00000000-0000-7000-8000-000000000000/repositories"
    body = {"owner_login": "acme", "name": "x", "external_id": 99, "github_installation_id": 1}
    assert env.client.post(url, json=body, headers=env.auth("owner-a")).status_code == 404


# ------------------------------------------------------------------ evidence
def test_mutations_are_audited(env):
    p = new_project(env, env.a, "owner-a", "audited")
    with psycopg.connect(env.db.owner_conninfo) as conn:
        rows = conn.execute("SELECT action, actor_type FROM audit_events WHERE target_id = %s", (p["id"],)).fetchall()
    assert ("project.created", "user") in rows


def test_runtime_refuses_owner_role(provisioned_db, env):
    app = create_app(make_settings(provisioned_db, env.jwks, database_url=provisioned_db.owner_async_url))
    with pytest.raises(RuntimeRoleError, match="unsafe for runtime use"), TestClient(app):
        pass


def test_logs_contain_no_secrets(env):
    """Runs last in this module: by now the log holds every request above."""
    env.client.get("/v1/me", headers=env.auth("owner-a"))
    output = env.log.getvalue()
    assert output.count('"http.request"') > 20
    for token in env.tokens:
        assert token not in output
    assert env.db.app_password not in output
    for line in output.splitlines():
        json.loads(line)  # every line is valid JSON


def test_workflow_create_when_orchestrator_down(env):
    """Temporal unavailable: 503, the row is kept as 'pending', and a retry with the same key reuses it."""
    p = new_project(env, env.a, "owner-a", "orch-down")
    repo_url = f"/v1/orgs/{env.a}/projects/{p['id']}/repositories"
    repo = env.client.post(
        repo_url,
        json={"owner_login": "acme", "name": "svc", "external_id": 777001, "github_installation_id": 9},
        headers=env.auth("owner-a"),
    ).json()
    body = {"repository_id": repo["id"], "title": "T", "request": "Do it", "idempotency_key": "orch-down-0001"}
    url = f"/v1/orgs/{env.a}/projects/{p['id']}/workflows"
    for _ in range(2):
        r = env.client.post(url, json=body, headers=env.auth("owner-a"))
        assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
    with psycopg.connect(env.db.owner_conninfo) as conn:
        rows = conn.execute("SELECT status FROM workflows WHERE idempotency_key = 'orch-down-0001'").fetchall()
    assert rows == [("pending",)]
