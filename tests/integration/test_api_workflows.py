"""Workflow and approval endpoints (and the `aieos` CLI) against real PostgreSQL, with a fake orchestrator."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from aieos.api.app import create_app
from aieos.cli import admin
from aieos.cli.main import Client, run
from aieos.core.settings import Settings

ISS, AUD = "https://issuer.test", "aieos-api"


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_signals = False

    async def start_delivery(self, tid: str, start: Any) -> None:
        self.calls.append(("start", tid, start))

    async def signal_approval(self, tid: str, signal: Any) -> None:
        if self.fail_signals:
            raise ConnectionError("temporal down")
        self.calls.append(("approval", tid, signal))

    async def signal_clarification(self, tid: str, answers: Any) -> None:
        self.calls.append(("clarification", tid, answers))

    async def request_cancel(self, tid: str, reason: str) -> None:
        self.calls.append(("cancel", tid, reason))


@pytest.fixture(scope="module")
def env(provisioned_db, published_agents, tmp_path_factory):
    keys = tmp_path_factory.mktemp("wf-keys")
    jwks = admin.dev_keys(keys)
    owner = provisioned_db.owner_conninfo
    org = admin.create_org(owner, slug="wfapi", name="WF", owner_sub="wf-owner", owner_email="o@wf.test")
    users: dict[str, Any] = {}
    for role in ("admin", "approver", "member", "viewer"):
        users[role] = admin.add_member(owner, org_slug="wfapi", sub=f"wf-{role}", email=f"{role}@wf.test", role=role)
    admin.add_member(owner, org_slug="wfapi", sub="wf-member2", email="m2@wf.test", role="member")
    with psycopg.connect(owner) as c:
        project = c.execute(
            "INSERT INTO projects (id, org_id, slug, name) VALUES (gen_random_uuid(), %s, 'core', 'Core') RETURNING id",
            (org,),
        ).fetchone()[0]
        repo = c.execute(
            """INSERT INTO repositories (id, org_id, project_id, external_id, owner_login, name,
               github_installation_id) VALUES (gen_random_uuid(), %s, %s, 4242, 'acme', 'app', 1) RETURNING id""",
            (org, project),
        ).fetchone()[0]
    settings = Settings(
        environment="test",
        database_url=provisioned_db.app_async_url,
        oidc_issuer=ISS,
        oidc_audience=AUD,
        oidc_jwks_path=jwks,
        _env_file=None,
    )
    orch = FakeOrchestrator()
    app = create_app(settings, log_stream=io.StringIO(), orchestrator=orch)

    def auth(sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {admin.dev_token(keys, issuer=ISS, audience=AUD, sub=sub)}"}

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, org=org, project=project, repo=repo, orch=orch, auth=auth, owner=owner, users=users
        )


def create(env, sub="wf-member", key=None, **over):
    body = {
        "repository_id": str(env.repo),
        "title": "CSV export",
        "request": "Add CSV export to reports",
        "idempotency_key": key or f"key-{uuid.uuid4().hex}",
        **over,
    }
    return env.client.post(f"/v1/orgs/{env.org}/projects/{env.project}/workflows", json=body, headers=env.auth(sub))


def sql(env, q, params=()):
    with psycopg.connect(env.owner) as c:
        cur = c.execute(q, params)
        return cur.fetchall() if cur.description else None


# ------------------------------------------------------------------ workflows
def test_create_workflow_starts_orchestration(env):
    r = create(env, key="create-once-001")
    assert r.status_code == 201, r.text
    wf = r.json()
    assert wf["status"] == "pending" and Decimal(wf["budget_usd"]) == Decimal("25")
    kind, tid, start = env.orch.calls[-1]
    assert kind == "start" and tid == f"feature-delivery-{wf['id']}" and start.request == "Add CSV export to reports"

    again = create(env, key="create-once-001")
    assert again.status_code == 200 and again.json()["id"] == wf["id"]  # idempotent
    assert sql(env, "SELECT count(*) FROM workflows WHERE idempotency_key = 'create-once-001'") == [(1,)]


@pytest.mark.parametrize(("role", "code"), [("viewer", 403), ("approver", 403), ("member", 201), ("admin", 201)])
def test_create_permissions(env, role, code):
    assert create(env, sub=f"wf-{role}").status_code == code


def test_create_rejects_foreign_repository_and_bad_budget(env):
    assert create(env, repository_id=str(uuid.uuid4())).status_code == 404
    assert create(env, budget_usd="5000").status_code == 422


def test_cancel_rules(env):
    wf = create(env).json()
    url = f"/v1/orgs/{env.org}/workflows/{wf['id']}/cancel"
    assert env.client.post(url, json={}, headers=env.auth("wf-member2")).status_code == 403  # not the creator
    assert env.client.post(url, json={"reason": "dup"}, headers=env.auth("wf-member")).status_code == 202
    assert env.orch.calls[-1] == ("cancel", f"feature-delivery-{wf['id']}", "dup")
    sql(env, "UPDATE workflows SET status = 'cancelled' WHERE id = %s", (wf["id"],))
    assert env.client.post(url, json={}, headers=env.auth("wf-admin")).status_code == 409


def test_clarification_only_when_waiting(env):
    wf = create(env).json()
    url = f"/v1/orgs/{env.org}/workflows/{wf['id']}/clarifications"
    body = {"answers": {"Q-1": "CSV"}}
    assert env.client.post(url, json=body, headers=env.auth("wf-member")).status_code == 409
    sql(env, "UPDATE workflows SET status = 'awaiting_input' WHERE id = %s", (wf["id"],))
    assert env.client.post(url, json=body, headers=env.auth("wf-member")).status_code == 202
    assert env.orch.calls[-1][2].answers == {"Q-1": "CSV"}


def test_list_and_get(env):
    wf = create(env).json()
    got = env.client.get(f"/v1/orgs/{env.org}/workflows/{wf['id']}", headers=env.auth("wf-viewer"))
    assert got.status_code == 200 and got.json()["title"] == "CSV export"
    listed = env.client.get(f"/v1/orgs/{env.org}/workflows?status=pending", headers=env.auth("wf-viewer")).json()
    assert wf["id"] in {w["id"] for w in listed}


# ------------------------------------------------------------------ approvals
def new_approval(env, *, level=3, requester=None, expires="1 hour", tool="github.merge_pull_request"):
    wf = create(env).json()
    ((aid,),) = sql(
        env,
        """INSERT INTO approvals (id, org_id, workflow_id, kind, permission_level, tool_name,
           request_digest, summary, requested_by_user_id, expires_at)
           VALUES (gen_random_uuid(), %s, %s, 'merge', %s, %s, %s, 'supervisor requests merge', %s,
                   now() + %s::interval) RETURNING id""",
        (env.org, wf["id"], level, tool, "a" * 64, requester, expires),
    )
    return str(aid), wf


def decide(env, aid, sub, decision="approve", version=1):
    return env.client.post(
        f"/v1/orgs/{env.org}/approvals/{aid}/decision",
        json={"decision": decision, "version": version, "comment": "LGTM"},
        headers=env.auth(sub),
    )


def test_approve_signals_workflow(env):
    aid, wf = new_approval(env)
    listed = env.client.get(f"/v1/orgs/{env.org}/approvals", headers=env.auth("wf-viewer")).json()
    assert aid in {a["id"] for a in listed}
    r = decide(env, aid, "wf-approver")
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["signal_delivered"] is True
    kind, tid, signal = env.orch.calls[-1]
    assert kind == "approval" and tid == f"feature-delivery-{wf['id']}" and signal.decision == "approved"
    assert decide(env, aid, "wf-approver", version=2).status_code == 409  # already decided
    assert sql(env, "SELECT action FROM audit_events WHERE target_id = %s", (aid,)) == [("approval.approved",)]


@pytest.mark.parametrize("sub", ["wf-member", "wf-admin", "wf-viewer"])
def test_non_approvers_cannot_decide(env, sub):
    aid, _ = new_approval(env)
    assert decide(env, aid, sub).status_code == 403


def test_stale_version_and_expiry(env):
    aid, _ = new_approval(env)
    assert decide(env, aid, "wf-approver", version=7).status_code == 409
    expired, _ = new_approval(env, expires="-1 minute")
    r = decide(env, expired, "wf-approver")
    assert r.status_code == 409 and "expired" in r.json()["error"]["message"]


def test_level4_four_eyes(env):
    aid, _ = new_approval(env, level=4, requester=env.users["approver"], tool="production.deploy")
    r = decide(env, aid, "wf-approver")
    assert r.status_code == 403 and "four-eyes" in r.json()["error"]["message"]
    assert decide(env, aid, "wf-owner").status_code == 200


def test_project_policy_can_narrow_approvers(env):
    sql(env, """UPDATE projects SET policy = '{"approver_roles": ["owner"]}'::jsonb WHERE id = %s""", (env.project,))
    try:
        aid, _ = new_approval(env)
        assert decide(env, aid, "wf-approver").status_code == 403
        assert decide(env, aid, "wf-owner").status_code == 200
    finally:
        sql(env, "UPDATE projects SET policy = '{}'::jsonb WHERE id = %s", (env.project,))


def test_decision_survives_signal_failure(env):
    aid, _ = new_approval(env)
    env.orch.fail_signals = True
    try:
        r = decide(env, aid, "wf-approver", decision="reject")
    finally:
        env.orch.fail_signals = False
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["signal_delivered"] is False
    assert sql(env, "SELECT status FROM approvals WHERE id = %s", (aid,)) == [("rejected",)]


def test_other_org_cannot_see_approvals(env):
    aid, _ = new_approval(env)
    other = admin.create_org(
        env.owner, slug="wf-other", name="O", owner_sub="wf-other-owner", owner_email="o@other.test"
    )
    assert env.client.get(f"/v1/orgs/{other}/approvals/{aid}", headers=env.auth("wf-other-owner")).status_code == 404
    r = env.client.post(
        f"/v1/orgs/{other}/approvals/{aid}/decision",
        json={"decision": "approve", "version": 1},
        headers=env.auth("wf-other-owner"),
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ CLI
def cli(env, sub, capsys, *argv):
    headers = env.auth(sub)

    def send(method, path, body):
        r = env.client.request(method, path, json=body, headers=headers)
        return r.status_code, (r.json() if r.content else None)

    code = run(list(argv), Client(send, str(env.org)))
    return code, capsys.readouterr()


def test_cli_list_and_approve(env, capsys):
    aid, _ = new_approval(env)
    code, out = cli(env, "wf-viewer", capsys, "approvals", "list")
    assert code == 0 and aid in out.out
    code, out = cli(env, "wf-approver", capsys, "approvals", "approve", aid, "--comment", "ship it")
    assert code == 0 and out.out.startswith("approved:")
    code, out = cli(env, "wf-approver", capsys, "approvals", "approve", aid)
    assert code == 1 and "HTTP 409" in out.err


def test_cli_answer_validation(env, capsys):
    wf = create(env).json()
    with pytest.raises(SystemExit):
        cli(env, "wf-member", capsys, "workflows", "answer", wf["id"], "--answer", "no-equals-sign")
