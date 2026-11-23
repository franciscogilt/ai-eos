"""ToolGateway against real PostgreSQL: authorization, idempotency, approvals, audit."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from moto import mock_aws

from aieos.artifacts.store import ArtifactStore, build_s3_client
from aieos.cli import admin
from aieos.contracts.tools import ToolRequest
from aieos.core.settings import StorageSettings
from aieos.gateway.catalog import load_catalog
from aieos.gateway.handlers import ToolInputError, ToolTransientError, artifact_handlers
from aieos.gateway.service import ToolGateway, ToolInProgressError
from aieos.persistence.db import Database
from tests.integration.conftest import ENGINEERING, seed_workflow

CATALOG = load_catalog(ENGINEERING / "tool-catalog.yaml")


@pytest.fixture(scope="module")
def world(provisioned_db, published_agents):
    owner = provisioned_db.owner_conninfo
    org = admin.create_org(owner, slug="gw", name="GW", owner_sub="gw-owner", owner_email="o@gw.test")
    approver = admin.add_member(owner, org_slug="gw", sub="gw-approver", email="a@gw.test", role="approver")
    member = admin.add_member(owner, org_slug="gw", sub="gw-member", email="m@gw.test", role="member")
    return {"org": org, "approver": approver, "member": member}


def runs(provisioned_db, org, *agents: str) -> dict[str, Any]:
    """One workflow with an agent run per requested agent key."""
    ids = seed_workflow(provisioned_db.owner_conninfo, org, agent_key=agents[0])
    out = {"workflow": ids["workflow"], "project": ids["project"], agents[0]: ids["run"]}
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        for key in agents[1:]:
            v = c.execute(
                """SELECT v.id FROM agent_versions v JOIN agents a ON a.id = v.agent_id
                             WHERE a.key = %s AND a.org_id IS NULL""",
                (key,),
            ).fetchone()[0]
            out[key] = c.execute(
                """INSERT INTO agent_runs (id, org_id, workflow_id, agent_version_id,
                                    model_provider, model_name) VALUES (gen_random_uuid(), %s, %s, %s, 'x', 'y')
                                    RETURNING id""",
                (org, ids["workflow"], v),
            ).fetchone()[0]
    return out


class Calls:
    def __init__(self) -> None:
        self.n = 0


def gateway_call(provisioned_db, req: ToolRequest, handlers: dict[str, Any], *, now=None):
    async def main():
        db = Database(provisioned_db.app_async_url, pool_size=2, max_overflow=0)
        try:
            return await ToolGateway(db, CATALOG, handlers, now=now).execute(req)
        finally:
            await db.dispose()

    return asyncio.run(main())


def counting(result: dict[str, Any] | Exception) -> tuple[Calls, Any]:
    calls = Calls()

    async def handler(ctx, args):
        calls.n += 1
        if isinstance(result, Exception):
            raise result
        return {**result, "agent": ctx.agent_key}

    return calls, handler


def req(org, w, run, tool, args=None, call_id="c1") -> ToolRequest:
    return ToolRequest(
        org_id=org, workflow_id=w["workflow"], agent_run_id=run, tool_name=tool, arguments=args or {}, call_id=call_id
    )


def owner_sql(provisioned_db, sql, params=()):
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def test_level2_executes_once_and_replays(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    calls, handler = counting({"passed": True})
    r = req(org, w, w["developer"], "sandbox.run_tests", {"command": "pytest"})
    first = gateway_call(provisioned_db, r, {"sandbox.run_tests": handler})
    second = gateway_call(provisioned_db, r, {"sandbox.run_tests": handler})
    assert first.status == second.status == "succeeded" and calls.n == 1
    assert first.result == second.result == {"passed": True, "agent": "developer"}
    assert first.tool_execution_id == second.tool_execution_id
    actions = [
        a
        for (a,) in owner_sql(
            provisioned_db,
            "SELECT action FROM audit_events WHERE target_id = %s ORDER BY chain_seq",
            (str(first.tool_execution_id),),
        )
    ]
    assert actions == ["tool.allowed", "tool.succeeded"]


def test_call_id_reuse_with_other_arguments_denied(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    _, handler = counting({})
    gateway_call(
        provisioned_db, req(org, w, w["developer"], "sandbox.run_tests", {"a": 1}), {"sandbox.run_tests": handler}
    )
    other = gateway_call(
        provisioned_db, req(org, w, w["developer"], "sandbox.run_tests", {"a": 2}), {"sandbox.run_tests": handler}
    )
    assert other.status == "denied" and "different arguments" in other.reason


def test_tool_not_in_contract_is_denied_without_running(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "requirements")
    calls, handler = counting({})
    res = gateway_call(
        provisioned_db,
        req(org, w, w["requirements"], "sandbox.run_command", {"cmd": "rm -rf /"}),
        {"sandbox.run_command": handler},
    )
    assert res.status == "denied" and "not granted" in res.reason and calls.n == 0
    assert owner_sql(
        provisioned_db, "SELECT status, decision FROM tool_executions WHERE id = %s", (res.tool_execution_id,)
    ) == [("denied", "deny")]


def test_unknown_run_is_denied(provisioned_db, world):
    import uuid

    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    res = gateway_call(provisioned_db, req(org, w, uuid.uuid4(), "sandbox.run_tests"), {})
    assert res.status == "denied" and res.tool_execution_id is None


def test_level3_approval_flow(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "supervisor")
    calls, handler = counting({"number": 12})
    handlers = {"github.create_pull_request": handler}
    r = req(org, w, w["supervisor"], "github.create_pull_request", {"head": "aieos/x"}, call_id="open-pr")

    first = gateway_call(provisioned_db, r, handlers)
    again = gateway_call(provisioned_db, r, handlers)
    assert first.status == again.status == "awaiting_approval" and calls.n == 0
    assert first.approval_id == again.approval_id  # no duplicate approval requests
    ((status, level, requester),) = owner_sql(
        provisioned_db,
        "SELECT status, permission_level, requested_by_user_id FROM approvals WHERE id = %s",
        (first.approval_id,),
    )
    assert (status, level, requester) == ("pending", 3, None)

    owner_sql(
        provisioned_db,
        "UPDATE approvals SET status='approved', decided_by_user_id=%s, decided_at=now() WHERE id=%s",
        (world["approver"], first.approval_id),
    )
    done = gateway_call(provisioned_db, r, handlers)
    assert done.status == "succeeded" and done.result["number"] == 12 and calls.n == 1
    assert str(done.approval_id) == str(first.approval_id)


def test_approval_by_non_approver_role_is_not_accepted(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "supervisor")
    calls, handler = counting({})
    r = req(org, w, w["supervisor"], "github.create_pull_request", {"head": "aieos/y"}, call_id="pr2")
    first = gateway_call(provisioned_db, r, {"github.create_pull_request": handler})
    owner_sql(
        provisioned_db,
        "UPDATE approvals SET status='approved', decided_by_user_id=%s, decided_at=now() WHERE id=%s",
        (world["member"], first.approval_id),
    )
    res = gateway_call(provisioned_db, r, {"github.create_pull_request": handler})
    assert res.status == "awaiting_approval" and "may not approve" in res.reason and calls.n == 0


def test_project_policy_auto_approves_level3(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "supervisor")
    owner_sql(
        provisioned_db,
        "UPDATE projects SET policy = %s::jsonb WHERE id = %s",
        (json.dumps({"auto_approved_level3_tools": ["github.create_pull_request"]}), w["project"]),
    )
    calls, handler = counting({"number": 3})
    res = gateway_call(
        provisioned_db,
        req(org, w, w["supervisor"], "github.create_pull_request", call_id="pr"),
        {"github.create_pull_request": handler},
    )
    assert res.status == "succeeded" and calls.n == 1


def test_missing_handler_fails_safely(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    res = gateway_call(provisioned_db, req(org, w, w["developer"], "sandbox.run_tests"), {})
    assert res.status == "failed" and "no handler" in res.reason


def test_handler_errors(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    _, bad_input = counting(ToolInputError("path outside workspace"))
    res = gateway_call(
        provisioned_db,
        req(org, w, w["developer"], "sandbox.write_file", call_id="w1"),
        {"sandbox.write_file": bad_input},
    )
    assert res.status == "failed" and "invalid input" in res.reason

    # Transient error on a NON-idempotent tool: failed, never silently re-run.
    _, flaky = counting(ToolTransientError("connection reset"))
    res = gateway_call(
        provisioned_db, req(org, w, w["developer"], "sandbox.run_command", call_id="r1"), {"sandbox.run_command": flaky}
    )
    assert res.status == "failed" and "non-idempotent" in res.reason

    # Transient error on an IDEMPOTENT tool: raised for the activity to retry; the lease is released.
    r = req(org, w, w["developer"], "sandbox.write_file", call_id="w2")
    with pytest.raises(ToolTransientError):
        gateway_call(provisioned_db, r, {"sandbox.write_file": flaky})
    ok_calls, ok = counting({"written": True})
    assert gateway_call(provisioned_db, r, {"sandbox.write_file": ok}).status == "succeeded" and ok_calls.n == 1


def test_running_lease(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    r = req(org, w, w["developer"], "sandbox.run_command", call_id="lease")
    _, handler = counting({})
    first = gateway_call(provisioned_db, r, {"sandbox.run_command": handler})
    owner_sql(
        provisioned_db,
        "UPDATE tool_executions SET status = 'running', updated_at = now() WHERE id = %s",
        (first.tool_execution_id,),
    )
    with pytest.raises(ToolInProgressError):
        gateway_call(provisioned_db, r, {"sandbox.run_command": handler})
    # After the lease, a non-idempotent call that was interrupted is failed, not re-run.
    later = datetime.now(UTC) + timedelta(minutes=16)
    res = gateway_call(provisioned_db, r, {"sandbox.run_command": handler}, now=lambda: later)
    assert res.status == "failed" and "interrupted" in res.reason


def test_budget_exhausted_denies(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    owner_sql(provisioned_db, "UPDATE workflows SET spent_usd = budget_usd WHERE id = %s", (w["workflow"],))
    res = gateway_call(provisioned_db, req(org, w, w["developer"], "sandbox.run_tests"), {})
    assert res.status == "denied" and "budget" in res.reason


def test_artifact_tools_through_gateway(provisioned_db, world):
    org = world["org"]
    w = runs(provisioned_db, org, "requirements")
    with mock_aws():
        s3 = build_s3_client(StorageSettings(s3_artifact_bucket="gw-bucket", _env_file=None))
        s3.create_bucket(Bucket="gw-bucket")

        async def main():
            db = Database(provisioned_db.app_async_url, pool_size=2, max_overflow=0)
            try:
                gw = ToolGateway(db, CATALOG, artifact_handlers(ArtifactStore(db, s3, bucket="gw-bucket")))
                wrote = await gw.execute(
                    req(
                        org,
                        w,
                        w["requirements"],
                        "artifacts.write",
                        {"kind": "requirements_spec", "content": "REQ-1: export CSV"},
                        "aw",
                    )
                )
                read = await gw.execute(
                    req(org, w, w["requirements"], "artifacts.read", {"artifact_id": wrote.result["artifact_id"]}, "ar")
                )
                return wrote, read
            finally:
                await db.dispose()

        wrote, read = asyncio.run(main())
    assert wrote.status == "succeeded" and read.result["text"] == "REQ-1: export CSV"


def test_supervisor_may_push_and_merge_only_with_approval(provisioned_db, world):
    """The orchestrator acts as the supervisor run; push and merge are granted to it (Level 3,
    so they wait for a human) and to no other agent."""
    org = world["org"]
    w = runs(provisioned_db, org, "supervisor")
    calls, handler = counting({})
    handlers = {"github.push_branch": handler, "github.merge_pull_request": handler}
    push = req(
        org,
        w,
        w["supervisor"],
        "github.push_branch",
        {"branch": "aieos/x", "patch_artifact_ids": []},
        call_id="push-branch",
    )
    merge = req(
        org,
        w,
        w["supervisor"],
        "github.merge_pull_request",
        {"pull_request": 1, "head_sha": "a" * 40},
        call_id="merge-pull-request",
    )
    for r in (push, merge):
        assert gateway_call(provisioned_db, r, handlers).status == "awaiting_approval"
    assert calls.n == 0
    dev = runs(provisioned_db, org, "developer")
    denied = gateway_call(
        provisioned_db,
        req(org, dev, dev["developer"], "github.merge_pull_request", {"pull_request": 1}, call_id="m2"),
        handlers,
    )
    assert denied.status == "denied" and "not granted" in denied.reason


def test_gateway_decisions_are_traced_without_arguments(provisioned_db, world):
    from opentelemetry.trace import StatusCode

    from tests.tracing import SpanCapture, all_attribute_text

    org = world["org"]
    w = runs(provisioned_db, org, "developer")
    _, handler = counting({"passed": True})
    with SpanCapture() as cap:
        gateway_call(
            provisioned_db,
            req(org, w, w["developer"], "sandbox.run_tests", {"command": "pytest -k ARG-CANARY-1"}),
            {"sandbox.run_tests": handler},
        )
        gateway_call(provisioned_db, req(org, w, w["developer"], "no.such_tool", call_id="c2"), {})
    ok, denied = cap.named("gateway ")
    assert ok.name == "gateway sandbox.run_tests" and ok.attributes["aieos.tool.status"] == "succeeded"
    assert ok.attributes["aieos.tool.permission_level"] == 2
    assert ok.attributes["aieos.workflow_id"] == str(w["workflow"])
    assert denied.attributes["aieos.tool.status"] == "denied" and denied.status.status_code is StatusCode.ERROR
    assert "ARG-CANARY-1" not in all_attribute_text(cap.spans)
