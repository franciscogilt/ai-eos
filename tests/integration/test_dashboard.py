"""Dashboard endpoints (/v1/me/orgs, timeline, costs) and the /ui static app, against real PostgreSQL."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from aieos.api.app import create_app
from aieos.cli import admin
from aieos.core.settings import Settings

from .conftest import seed_workflow

ISS, AUD = "https://issuer.test", "aieos-api"


@pytest.fixture(scope="module")
def env(provisioned_db, published_agents, tmp_path_factory):
    keys = tmp_path_factory.mktemp("dash-keys")
    jwks = admin.dev_keys(keys)
    owner = provisioned_db.owner_conninfo
    org_a = admin.create_org(owner, slug="dash-a", name="Dash A", owner_sub="dash-owner", owner_email="o@d.test")
    org_b = admin.create_org(owner, slug="dash-b", name="Dash B", owner_sub="dash-other", owner_email="x@d.test")
    admin.add_member(owner, org_slug="dash-b", sub="dash-owner", email="o@d.test", role="viewer")
    admin.add_member(owner, org_slug="dash-a", sub="dash-viewer", email="v@d.test", role="viewer")
    ids = seed_workflow(owner, org_a)
    other = seed_workflow(owner, org_b)
    with psycopg.connect(owner) as c:
        c.execute(
            """INSERT INTO workflow_steps (org_id, workflow_id, step_key, sequence, status, started_at)
               VALUES (%s, %s, 'requirements', 1, 'succeeded', now())""",
            (org_a, ids["workflow"]),
        )
        c.execute("UPDATE agent_runs SET step_key = 'requirements', step_attempt = 1 WHERE id = %s", (ids["run"],))
        task = c.execute(
            """INSERT INTO tasks (org_id, workflow_id, key, title, description, assigned_agent_key)
               VALUES (%s, %s, 'T1', 'Export', 'd', 'developer') RETURNING id""",
            (org_a, ids["workflow"]),
        ).fetchone()[0]
        c.execute(
            """INSERT INTO quality_gates (org_id, workflow_id, task_id, gate_key, result, details)
               VALUES (%s, %s, %s, 'tests_pass', 'passed', %s)""",
            (org_a, ids["workflow"], task, json.dumps({"summary": "12 passed"})),
        )
        for wf, run, org, cost, when in (
            (ids["workflow"], ids["run"], org_a, "0.25", "now()"),
            (ids["workflow"], ids["run"], org_a, "0.50", "now() - interval '3 days'"),
            (ids["workflow"], ids["run"], org_a, "9.00", "now() - interval '60 days'"),  # outside 30 days
            (other["workflow"], other["run"], org_b, "7.00", "now()"),  # another tenant
        ):
            c.execute(
                f"""INSERT INTO model_usage (org_id, workflow_id, agent_run_id, provider, model, input_tokens,
                       output_tokens, cost_usd, occurred_at)
                   VALUES (%s, %s, %s, 'openai', 'model-a', 100, 10, %s, {when})""",
                (org, wf, run, cost),
            )
    settings = Settings(
        environment="test",
        database_url=provisioned_db.app_async_url,
        oidc_issuer=ISS,
        oidc_audience=AUD,
        oidc_jwks_path=jwks,
        oidc_ui_client_id="dash-ui",
        oidc_ui_extra_origins="https://login.example.com, javascript:alert(1)",
        _env_file=None,
    )
    app = create_app(settings, log_stream=io.StringIO(), orchestrator=None)

    def auth(sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {admin.dev_token(keys, issuer=ISS, audience=AUD, sub=sub)}"}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, a=org_a, b=org_b, ids=ids, other=other, auth=auth)


def get(env: Any, path: str, sub: str = "dash-owner") -> Any:
    return env.client.get(path, headers=env.auth(sub))


def test_my_orgs_lists_only_own_memberships(env):
    r = get(env, "/v1/me/orgs")
    assert r.status_code == 200
    assert {(o["slug"], o["role"]) for o in r.json()} == {("dash-a", "owner"), ("dash-b", "viewer")}
    assert [o["slug"] for o in get(env, "/v1/me/orgs", "dash-viewer").json()] == ["dash-a"]
    assert env.client.get("/v1/me/orgs").status_code == 401


def test_timeline(env):
    r = get(env, f"/v1/orgs/{env.a}/workflows/{env.ids['workflow']}/timeline", "dash-viewer")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["workflow"]["id"] == str(env.ids["workflow"])
    assert [(s["step_key"], s["status"]) for s in t["steps"]] == [("requirements", "succeeded")]
    assert [(k["key"], k["assigned_agent_key"]) for k in t["tasks"]] == [("T1", "developer")]
    assert t["gates"][0]["task_key"] == "T1" and t["gates"][0]["details"] == {"summary": "12 passed"}
    (run,) = t["runs"]
    assert run["agent_key"] == "developer" and run["step_key"] == "requirements"
    assert Decimal(run["cost_usd"]) == Decimal("9.75") and run["input_tokens"] == 300


def test_timeline_is_tenant_scoped(env):
    # A workflow of org B looked up through org A is invisible (RLS), and a non-member gets 404 too
    # (org ids cannot be probed).
    assert get(env, f"/v1/orgs/{env.a}/workflows/{env.other['workflow']}/timeline").status_code == 404
    assert get(env, f"/v1/orgs/{env.b}/workflows/{env.other['workflow']}/timeline", "dash-viewer").status_code == 404


def test_costs(env):
    c = get(env, f"/v1/orgs/{env.a}/costs?days=30", "dash-viewer").json()
    assert Decimal(c["total_usd"]) == Decimal("0.75")  # 60-day-old row and the other tenant excluded
    assert [(r["key"], r["calls"]) for r in c["by_agent"]] == [("developer", 2)]
    assert [r["key"] for r in c["by_model"]] == ["openai/model-a"]
    assert len(c["by_day"]) == 2
    assert Decimal(get(env, f"/v1/orgs/{env.a}/costs?days=90").json()["total_usd"]) == Decimal("9.75")
    assert get(env, f"/v1/orgs/{env.a}/costs?days=0").status_code == 422


def test_ui_assets_have_strict_headers(env):
    assert env.client.get("/ui", follow_redirects=False).status_code == 307
    for path, kind in (("/ui/", "text/html"), ("/ui/app.js", "text/javascript"), ("/ui/app.css", "text/css")):
        r = env.client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
        assert "connect-src 'self' https://issuer.test https://login.example.com;" in csp  # bad origin dropped
        assert r.headers["x-frame-options"] == "DENY"
    assert env.client.get("/ui/../pyproject.toml").status_code == 404
    assert env.client.get("/ui/secrets.json").status_code == 404


def test_ui_config_is_public_and_has_no_secrets(env):
    r = env.client.get("/ui/config.json")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "issuer": ISS,
        "client_id": "dash-ui",
        "scopes": "openid profile email",
        "audience": None,
        "token_paste": True,  # only in local/test, or when no browser client is configured
    }


def test_ui_never_renders_data_as_html():
    from importlib import resources

    js = resources.files("aieos.api").joinpath("ui", "app.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js
