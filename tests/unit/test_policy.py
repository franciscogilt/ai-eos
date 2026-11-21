from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aieos.agents.registry import load_all
from aieos.gateway.policy import (
    ApprovalRecord,
    Decision,
    ProjectPolicy,
    ToolCallContext,
    evaluate,
    request_digest,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def contracts(definitions_dir, prompts_dir, catalog):
    return {k: v.contract for k, v in load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog).items()}


def ctx(tool: str, args: dict | None = None, *, usd: float = 10.0, tokens: int = 1000) -> ToolCallContext:
    return ToolCallContext(
        org_id="org-1",
        workflow_id="wf-1",
        agent_run_id="run-1",
        tool_name=tool,
        arguments=args or {},
        budget_remaining_usd=usd,
        budget_remaining_tokens=tokens,
    )


def approval(tool: str, args: dict, **overrides) -> ApprovalRecord:
    base = dict(
        approval_id="ap-1",
        status="approved",
        tool_name=tool,
        request_digest=request_digest(tool, args, "wf-1"),
        permission_level=3,
        expires_at=NOW + timedelta(hours=1),
        requested_by_user_id=None,
        decided_by_user_id="user-approver",
        decided_by_role="approver",
    )
    base.update(overrides)
    return ApprovalRecord(**base)


DEFAULT_POLICY = ProjectPolicy()


def run(contract, catalog, call, policy=DEFAULT_POLICY, approvals=()):
    return evaluate(call, contract=contract, catalog=catalog, policy=policy, approvals=approvals, now=NOW)


def test_level1_allowed(contracts, catalog):
    d = run(contracts["requirements"], catalog, ctx("repo.read_file", {"path": "README.md"}))
    assert d.decision is Decision.ALLOW


def test_level2_allowed_for_developer(contracts, catalog):
    assert run(contracts["developer"], catalog, ctx("sandbox.run_tests")).decision is Decision.ALLOW


def test_unknown_tool_denied(contracts, catalog):
    d = run(contracts["developer"], catalog, ctx("shell.exec"))
    assert d.decision is Decision.DENY and "unknown tool" in d.reason


def test_tool_not_granted_denied_even_if_low_level(contracts, catalog):
    # Requirements agent has no sandbox tools.
    d = run(contracts["requirements"], catalog, ctx("sandbox.run_command"))
    assert d.decision is Decision.DENY and "not granted" in d.reason


def test_developer_cannot_create_pr(contracts, catalog):
    d = run(contracts["developer"], catalog, ctx("github.create_pull_request"))
    assert d.decision is Decision.DENY


def test_budget_exhausted_denies(contracts, catalog):
    d = run(contracts["developer"], catalog, ctx("sandbox.run_tests", usd=0))
    assert d.decision is Decision.DENY and "budget" in d.reason


def test_level3_requires_approval_by_default(contracts, catalog):
    d = run(contracts["supervisor"], catalog, ctx("github.create_pull_request", {"branch": "aieos/x"}))
    assert d.decision is Decision.REQUIRE_APPROVAL


def test_level3_auto_approved_by_policy(contracts, catalog):
    policy = ProjectPolicy(auto_approved_level3_tools=frozenset({"github.create_pull_request"}))
    d = run(contracts["supervisor"], catalog, ctx("github.create_pull_request"), policy=policy)
    assert d.decision is Decision.ALLOW


def test_matching_approval_allows(contracts, catalog):
    args = {"branch": "aieos/wf-1/T-1"}
    d = run(
        contracts["supervisor"],
        catalog,
        ctx("github.create_pull_request", args),
        approvals=(approval("github.create_pull_request", args),),
    )
    assert d.decision is Decision.ALLOW and d.approval_id == "ap-1"


def test_approval_bound_to_exact_arguments(contracts, catalog):
    approved = approval("github.create_pull_request", {"branch": "aieos/a"})
    d = run(
        contracts["supervisor"],
        catalog,
        ctx("github.create_pull_request", {"branch": "aieos/b"}),
        approvals=(approved,),
    )
    assert d.decision is Decision.REQUIRE_APPROVAL and "does not match" in d.reason


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"status": "rejected"}, "rejected"),
        ({"expires_at": NOW - timedelta(seconds=1)}, "expired"),
        ({"decided_by_role": "member"}, "may not approve"),
        ({"decided_by_user_id": None}, "no human decider"),
    ],
)
def test_invalid_approvals_rejected(contracts, catalog, override, fragment):
    args = {"branch": "aieos/x"}
    d = run(
        contracts["supervisor"],
        catalog,
        ctx("github.create_pull_request", args),
        approvals=(approval("github.create_pull_request", args, **override),),
    )
    assert d.decision is Decision.REQUIRE_APPROVAL and fragment in d.reason


def test_digest_is_order_independent():
    assert request_digest("t.x", {"a": 1, "b": 2}, "wf") == request_digest("t.x", {"b": 2, "a": 1}, "wf")
    assert request_digest("t.x", {"a": 1}, "wf") != request_digest("t.x", {"a": 1}, "wf-other")
