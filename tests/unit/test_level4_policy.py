"""Level 4 rules, tested with a synthetic release-manager contract (no MVP agent has Level 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aieos.contracts.agent_contract import AgentContract
from aieos.gateway.policy import ApprovalRecord, Decision, ProjectPolicy, ToolCallContext, evaluate, request_digest

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
ARGS = {"commit_sha": "a" * 40, "environment": "production"}


@pytest.fixture
def release_manager() -> AgentContract:
    return AgentContract.model_validate(
        {
            "api_version": "aieos/v1",
            "key": "release_manager",
            "name": "Release Manager",
            "version": "0.1.0",
            "purpose": "Synthetic contract used only by tests.",
            "responsibilities": ["deploy"],
            "instructions_path": "x.md",
            "input_schema": "SupervisorInput",
            "output_schema": "SupervisorDecision",
            "model": {"profile": "fast", "primary": {"provider": "openai", "model": "m"}},
            "tools": ["production.deploy"],
            "max_permission_level": 4,
            "limits": {
                "max_turns": 1,
                "max_tool_calls": 1,
                "max_total_tokens": 1,
                "max_cost_usd": 1,
                "timeout_seconds": 1,
            },
            "failure_handling": {"max_retries": 0, "retry_on": [], "on_exhausted": "request_human"},
        }
    )


def call() -> ToolCallContext:
    return ToolCallContext("org-1", "wf-1", "run-1", "production.deploy", ARGS, 5.0, 100)


def ap(**kw) -> ApprovalRecord:
    base = dict(
        approval_id="ap-9",
        status="approved",
        tool_name="production.deploy",
        request_digest=request_digest("production.deploy", ARGS, "wf-1"),
        permission_level=4,
        expires_at=NOW + timedelta(minutes=30),
        requested_by_user_id="alice",
        decided_by_user_id="bob",
        decided_by_role="approver",
    )
    base.update(kw)
    return ApprovalRecord(**base)


def test_level4_never_auto_approved(release_manager, catalog):
    policy = ProjectPolicy(auto_approved_level3_tools=frozenset({"production.deploy"}))
    d = evaluate(call(), contract=release_manager, catalog=catalog, policy=policy, approvals=(), now=NOW)
    assert d.decision is Decision.REQUIRE_APPROVAL


def test_level4_with_human_approval_allowed(release_manager, catalog):
    d = evaluate(call(), contract=release_manager, catalog=catalog, policy=ProjectPolicy(), approvals=(ap(),), now=NOW)
    assert d.decision is Decision.ALLOW


def test_level4_four_eyes(release_manager, catalog):
    d = evaluate(
        call(),
        contract=release_manager,
        catalog=catalog,
        policy=ProjectPolicy(),
        approvals=(ap(decided_by_user_id="alice"),),
        now=NOW,
    )
    assert d.decision is Decision.REQUIRE_APPROVAL and "four-eyes" in d.reason


def test_level3_approval_cannot_authorize_level4(release_manager, catalog):
    d = evaluate(
        call(),
        contract=release_manager,
        catalog=catalog,
        policy=ProjectPolicy(),
        approvals=(ap(permission_level=3),),
        now=NOW,
    )
    assert d.decision is Decision.REQUIRE_APPROVAL


def test_level4_kill_switch(release_manager, catalog):
    d = evaluate(
        call(),
        contract=release_manager,
        catalog=catalog,
        policy=ProjectPolicy(allow_level4_requests=False),
        approvals=(ap(),),
        now=NOW,
    )
    assert d.decision is Decision.DENY
