"""Runtime authorization for agent tool calls.

`evaluate()` is a pure, deterministic function: same inputs -> same decision.
It runs in the tool-gateway worker before any tool executes, and its result is
persisted to `tool_executions` and `audit_events`. Prompts are never consulted.

Decision order (first match wins):
  1. Unknown tool                                  -> deny
  2. Tool not granted by the agent's contract      -> deny
  3. Tool level above contract max level           -> deny
  4. Workflow budget exhausted                     -> deny
  5. Level 1-2                                     -> allow
  6. Level 4 disabled by project policy            -> deny
  7. Valid approval matching this exact request    -> allow
  8. Level 3 auto-approved by project policy       -> allow
  9. Otherwise                                     -> require_approval
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aieos.contracts.agent_contract import AgentContract, PermissionLevel
from aieos.gateway.catalog import ToolCatalog


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ProjectPolicy(BaseModel):
    """Stored in `projects.policy`. Defaults are the safe choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Level 3 tools that may run without a human (e.g. github.push_branch,
    # github.create_pull_request). Merge and staging deploy are NOT auto by default.
    auto_approved_level3_tools: frozenset[str] = frozenset()
    # Kill switch: when False, agents cannot even request Level 4 operations.
    allow_level4_requests: bool = True
    approver_roles: frozenset[str] = frozenset({"owner", "approver"})


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Projection of an `approvals` row, as seen by the gateway."""

    approval_id: str
    status: str
    tool_name: str | None
    request_digest: str
    permission_level: int
    expires_at: datetime
    requested_by_user_id: str | None
    decided_by_user_id: str | None
    decided_by_role: str | None


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    org_id: str
    workflow_id: str
    agent_run_id: str
    tool_name: str
    arguments: dict[str, Any]
    budget_remaining_usd: float
    budget_remaining_tokens: int


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str
    permission_level: PermissionLevel | None
    request_digest: str
    approval_id: str | None = None


def request_digest(tool_name: str, arguments: dict[str, Any], workflow_id: str) -> str:
    """Stable SHA-256 over the exact request. An approval is bound to this digest,
    so approving "deploy commit abc" cannot be replayed for "deploy commit def"."""
    canonical = json.dumps(
        {"tool": tool_name, "workflow_id": workflow_id, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_is_valid(
    approval: ApprovalRecord,
    *,
    tool_name: str,
    digest: str,
    level: PermissionLevel,
    policy: ProjectPolicy,
    now: datetime,
) -> tuple[bool, str]:
    if approval.status != "approved":
        return False, f"approval {approval.approval_id} is {approval.status}"
    if approval.tool_name != tool_name or approval.request_digest != digest:
        return False, f"approval {approval.approval_id} does not match this request"
    if approval.permission_level < int(level):
        return False, f"approval {approval.approval_id} was granted for a lower level"
    if now >= approval.expires_at:
        return False, f"approval {approval.approval_id} expired"
    if approval.decided_by_user_id is None:
        return False, "approval has no human decider"
    if approval.decided_by_role not in policy.approver_roles:
        return False, f"decider role {approval.decided_by_role!r} may not approve"
    if level == PermissionLevel.SENSITIVE and approval.requested_by_user_id == approval.decided_by_user_id:
        return False, "four-eyes rule: requester cannot approve a Level 4 operation"
    return True, "approved"


def evaluate(
    ctx: ToolCallContext,
    *,
    contract: AgentContract,
    catalog: ToolCatalog,
    policy: ProjectPolicy,
    approvals: tuple[ApprovalRecord, ...],
    now: datetime,
) -> PolicyDecision:
    digest = request_digest(ctx.tool_name, ctx.arguments, ctx.workflow_id)
    tool = catalog.get(ctx.tool_name)

    if tool is None:
        return PolicyDecision(Decision.DENY, f"unknown tool {ctx.tool_name!r}", None, digest)
    level = tool.level

    if ctx.tool_name not in contract.tools:
        return PolicyDecision(
            Decision.DENY, f"agent {contract.key}@{contract.version} is not granted {ctx.tool_name}", level, digest
        )
    if level > contract.max_permission_level:
        return PolicyDecision(
            Decision.DENY,
            f"{ctx.tool_name} is level {int(level)}; contract max is {int(contract.max_permission_level)}",
            level,
            digest,
        )
    if ctx.budget_remaining_usd <= 0 or ctx.budget_remaining_tokens <= 0:
        return PolicyDecision(Decision.DENY, "workflow budget exhausted", level, digest)

    if level <= PermissionLevel.ISOLATED_EXECUTION:
        return PolicyDecision(Decision.ALLOW, f"level {int(level)} auto-allowed", level, digest)

    if level == PermissionLevel.SENSITIVE and not policy.allow_level4_requests:
        return PolicyDecision(Decision.DENY, "level 4 requests disabled by project policy", level, digest)

    rejections: list[str] = []
    for approval in approvals:
        ok, why = _approval_is_valid(
            approval, tool_name=ctx.tool_name, digest=digest, level=level, policy=policy, now=now
        )
        if ok:
            return PolicyDecision(
                Decision.ALLOW, f"human approval {approval.approval_id}", level, digest, approval.approval_id
            )
        rejections.append(why)

    if level == PermissionLevel.CONTROLLED_SHARED and ctx.tool_name in policy.auto_approved_level3_tools:
        return PolicyDecision(Decision.ALLOW, "level 3 auto-approved by project policy", level, digest)

    reason = "human approval required"
    if rejections:
        reason += f" ({'; '.join(rejections)})"
    return PolicyDecision(Decision.REQUIRE_APPROVAL, reason, level, digest)


class ApprovalRequestSpec(BaseModel):
    """What the gateway asks the orchestrator to create when a call needs approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    permission_level: int = Field(ge=3, le=4)
    request_digest: str
    summary: str
    ttl_minutes: int
