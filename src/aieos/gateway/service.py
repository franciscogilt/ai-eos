"""ToolGateway: authorize, de-duplicate, approve, execute, record. Every agent tool call goes here.

Per call:
  1. Lock (or create) the `tool_executions` row for the call's idempotency key.
     A finished call returns its stored result; it is never executed twice.
  2. Load the agent's *stored* contract, the project policy, the workflow budget and
     matching approvals, then run the pure policy engine.
  3. deny -> record. require_approval -> create (or reuse) one pending approval.
     allow -> mark running, run the handler outside the transaction, record the result.
Every decision and execution is an audit event.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aieos.contracts.agent_contract import AgentContract, PermissionLevel
from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.core.ids import uuid7
from aieos.core.logging import get_logger, redact_value
from aieos.gateway.catalog import ToolCatalog, ToolSpec
from aieos.gateway.handlers import Handler, ToolContext, ToolInputError, ToolTransientError
from aieos.gateway.policy import (
    ApprovalRecord,
    Decision,
    PolicyDecision,
    ProjectPolicy,
    ToolCallContext,
    evaluate,
    request_digest,
)
from aieos.persistence import repositories as repo
from aieos.persistence.db import Database

log = get_logger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "denied"})
RUNNING_LEASE = timedelta(minutes=15)  # matches the execute_tool activity timeout
MAX_RESULT_BYTES = 60_000


class ToolInProgressError(RuntimeError):
    """Another attempt holds this call; the activity retries later."""


class _Ctx:
    def __init__(self, row: Any) -> None:
        self.agent_key: str = row.agent_key
        self.contract = AgentContract.model_validate(row.contract)
        self.policy = ProjectPolicy.model_validate(row.policy or {})
        self.created_by: UUID = row.created_by
        self.remaining_usd = float(Decimal(row.budget_usd) - Decimal(row.spent_usd))
        self.remaining_tokens = int(row.budget_tokens) - int(row.spent_tokens)


def _summary(tool: str, agent_key: str, args: dict[str, Any]) -> str:
    rendered = json.dumps(redact_value(args), sort_keys=True, default=str)
    if len(rendered) > 500:
        rendered = rendered[:497] + "..."
    return f"{agent_key} requests {tool} with {rendered}"


class ToolGateway:
    def __init__(self, db: Database, catalog: ToolCatalog, handlers: dict[str, Handler], *, now: Any = None) -> None:
        self._db = db
        self._catalog = catalog
        self._handlers = handlers
        self._now = now or (lambda: datetime.now(UTC))

    async def execute(self, req: ToolRequest) -> ToolResult:
        tool = self._catalog.get(req.tool_name)
        digest = request_digest(req.tool_name, req.arguments, str(req.workflow_id))
        level = int(tool.level) if tool else int(PermissionLevel.SENSITIVE)

        async with self._db.tenant(req.org_id) as conn:
            ctx_row = (
                await conn.execute(
                    text(
                        """SELECT a.key AS agent_key, v.contract, p.policy, w.created_by, w.budget_usd, w.spent_usd,
                          w.budget_tokens, w.spent_tokens
                   FROM agent_runs r
                   JOIN agent_versions v ON v.id = r.agent_version_id
                   JOIN agents a ON a.id = v.agent_id
                   JOIN workflows w ON w.id = r.workflow_id
                   JOIN projects p ON p.id = w.project_id
                   WHERE r.id = :run AND r.workflow_id = :wf"""
                    ),
                    {"run": req.agent_run_id, "wf": req.workflow_id},
                )
            ).first()
            if ctx_row is None:
                log.warning("tool.unknown_run", tool=req.tool_name)
                return ToolResult(status="denied", reason="unknown agent run for this workflow")
            ctx = _Ctx(ctx_row)

            existing = (
                await conn.execute(
                    text(
                        """SELECT id, status, decision_reason, result, approval_id, request_digest, updated_at
                   FROM tool_executions WHERE idempotency_key = :k FOR UPDATE"""
                    ),
                    {"k": req.idempotency_key},
                )
            ).first()
            if existing is not None:
                if existing.request_digest != digest:
                    return ToolResult(
                        status="denied", tool_execution_id=existing.id, reason="call id reused with different arguments"
                    )
                if existing.status in TERMINAL:
                    return ToolResult(
                        status=existing.status,
                        tool_execution_id=existing.id,
                        reason=existing.decision_reason,
                        result=existing.result,
                        approval_id=existing.approval_id,
                    )
                if existing.status == "running":
                    if self._now() - existing.updated_at < RUNNING_LEASE:
                        raise ToolInProgressError(f"tool call {existing.id} is already running")
                    if tool is None or not tool.idempotent:
                        await self._finish(
                            conn, req, existing.id, "failed", "interrupted; tool is not idempotent", None, level
                        )
                        return ToolResult(
                            status="failed",
                            tool_execution_id=existing.id,
                            reason="interrupted; tool is not idempotent, so it was not re-run",
                        )

            approvals = await self._approvals(conn, req, digest)
            decision = evaluate(
                ToolCallContext(
                    org_id=str(req.org_id),
                    workflow_id=str(req.workflow_id),
                    agent_run_id=str(req.agent_run_id),
                    tool_name=req.tool_name,
                    arguments=req.arguments,
                    budget_remaining_usd=ctx.remaining_usd,
                    budget_remaining_tokens=ctx.remaining_tokens,
                ),
                contract=ctx.contract,
                catalog=self._catalog,
                policy=ctx.policy,
                approvals=approvals,
                now=self._now(),
            )
            execution_id = existing.id if existing is not None else uuid7()

            if decision.decision is Decision.DENY:
                await self._upsert(conn, req, execution_id, level, decision, "denied", None)
                await self._audit(conn, req, execution_id, "tool.denied", decision.reason)
                return ToolResult(status="denied", tool_execution_id=execution_id, reason=decision.reason)

            if decision.decision is Decision.REQUIRE_APPROVAL:
                assert tool is not None  # unknown tools are always denied
                approval_id = await self._pending_approval(conn, req, ctx, tool, digest, approvals)
                await self._upsert(conn, req, execution_id, level, decision, "awaiting_approval", approval_id)
                await self._audit(
                    conn, req, execution_id, "tool.approval_requested", decision.reason, approval_id=approval_id
                )
                return ToolResult(
                    status="awaiting_approval",
                    tool_execution_id=execution_id,
                    reason=decision.reason,
                    approval_id=approval_id,
                )

            assert tool is not None
            handler = self._handlers.get(req.tool_name)
            if handler is None:
                await self._upsert(conn, req, execution_id, level, decision, "failed", decision.approval_id)
                await self._finish(
                    conn, req, execution_id, "failed", "no handler registered for this tool", None, level
                )
                return ToolResult(
                    status="failed", tool_execution_id=execution_id, reason="no handler registered for this tool"
                )
            await self._upsert(conn, req, execution_id, level, decision, "running", decision.approval_id)
            await self._audit(
                conn, req, execution_id, "tool.allowed", decision.reason, approval_id=decision.approval_id
            )

        # Outside the transaction: tools can be slow, and no row lock is held while they run.
        tool_ctx = ToolContext(req.org_id, req.workflow_id, req.agent_run_id, ctx.agent_key, tool)
        try:
            result = await handler(tool_ctx, req.arguments)
            if len(json.dumps(result, default=str)) > MAX_RESULT_BYTES:
                raise ToolInputError("tool result too large to store inline; return an artifact instead")
            status, reason = "succeeded", decision.reason
        except ToolTransientError as exc:
            if tool.idempotent:
                async with self._db.tenant(req.org_id) as conn:  # release the lease so a retry may run it
                    await conn.execute(
                        text("UPDATE tool_executions SET status = 'pending', updated_at = now() WHERE id = :id"),
                        {"id": execution_id},
                    )
                raise
            status, reason, result = "failed", f"transient error on a non-idempotent tool: {exc}", None
        except ToolInputError as exc:
            status, reason, result = "failed", f"invalid input: {exc}", None
        except Exception as exc:
            log.exception("tool.handler_error", tool=req.tool_name)
            status, reason, result = "failed", f"tool error: {type(exc).__name__}", None

        async with self._db.tenant(req.org_id) as conn:
            await self._finish(conn, req, execution_id, status, reason, result, level)
        return ToolResult(
            status=status,
            tool_execution_id=execution_id,
            reason=reason,
            result=result,
            approval_id=decision.approval_id,
        )

    # ------------------------------------------------------------------ helpers
    async def _approvals(self, conn: AsyncConnection, req: ToolRequest, digest: str) -> tuple[ApprovalRecord, ...]:
        rows = await conn.execute(
            text(
                """SELECT a.id, a.status, a.tool_name, a.request_digest, a.permission_level, a.expires_at,
                      a.requested_by_user_id, a.decided_by_user_id, m.role AS decided_by_role
               FROM approvals a
               LEFT JOIN memberships m ON m.user_id = a.decided_by_user_id
               WHERE a.workflow_id = :wf AND a.tool_name = :tool AND a.request_digest = :digest
               ORDER BY a.created_at"""
            ),
            {"wf": req.workflow_id, "tool": req.tool_name, "digest": digest},
        )
        return tuple(
            ApprovalRecord(
                approval_id=str(r.id),
                status=r.status,
                tool_name=r.tool_name,
                request_digest=r.request_digest,
                permission_level=r.permission_level,
                expires_at=r.expires_at,
                requested_by_user_id=str(r.requested_by_user_id) if r.requested_by_user_id else None,
                decided_by_user_id=str(r.decided_by_user_id) if r.decided_by_user_id else None,
                decided_by_role=r.decided_by_role,
            )
            for r in rows
        )

    async def _pending_approval(
        self,
        conn: AsyncConnection,
        req: ToolRequest,
        ctx: _Ctx,
        tool: ToolSpec,
        digest: str,
        approvals: tuple[ApprovalRecord, ...],
    ) -> UUID:
        now = self._now()
        for a in approvals:
            if a.status == "pending" and a.expires_at > now:
                return UUID(a.approval_id)
        # An expired pending approval is closed first, so the partial unique index allows a new one.
        await conn.execute(
            text(
                "UPDATE approvals SET status = 'expired', version = version + 1 "
                "WHERE workflow_id = :wf AND tool_name = :tool AND request_digest = :d "
                "AND status = 'pending'"
            ),
            {"wf": req.workflow_id, "tool": req.tool_name, "d": digest},
        )
        approval_id = uuid7()
        kind = {
            "github.merge_pull_request": "merge",
            "staging.deploy": "staging_deploy",
            "production.deploy": "production_deploy",
            "policy.exception": "policy_exception",
        }.get(req.tool_name, "tool_execution")
        sensitive = tool.level >= PermissionLevel.SENSITIVE
        await conn.execute(
            text(
                """INSERT INTO approvals (id, org_id, workflow_id, kind, permission_level, tool_name, request_digest,
                                      summary, requested_by_agent_run_id, requested_by_user_id, expires_at)
               VALUES (:id, :org, :wf, :kind, :level, :tool, :digest, :summary, :run, :requester, :expires)"""
            ),
            {
                "id": approval_id,
                "org": req.org_id,
                "wf": req.workflow_id,
                "kind": kind,
                "level": int(tool.level),
                "tool": req.tool_name,
                "digest": digest,
                "summary": _summary(req.tool_name, ctx.agent_key, req.arguments),
                "run": req.agent_run_id,
                # Level 4: the workflow's creator is the accountable requester (four-eyes applies).
                "requester": ctx.created_by if sensitive else None,
                "expires": now + timedelta(minutes=tool.approval_ttl_minutes or 60),
            },
        )
        await repo.record_audit(
            conn,
            org_id=req.org_id,
            actor_type="agent",
            actor_id=str(req.agent_run_id),
            action="approval.requested",
            target_type="approval",
            target_id=str(approval_id),
            payload={"tool": req.tool_name, "level": int(tool.level)},
        )
        return approval_id

    async def _upsert(
        self,
        conn: AsyncConnection,
        req: ToolRequest,
        execution_id: UUID,
        level: int,
        decision: PolicyDecision,
        status: str,
        approval_id: str | UUID | None,
    ) -> None:
        await conn.execute(
            text(
                """INSERT INTO tool_executions (id, org_id, workflow_id, agent_run_id, tool_name, permission_level,
                                            decision, decision_reason, approval_id, idempotency_key, request_digest,
                                            status)
               VALUES (:id, :org, :wf, :run, :tool, :level, :decision, :reason, :approval, :key, :digest, :status)
               ON CONFLICT (org_id, idempotency_key) DO UPDATE
                 SET decision = EXCLUDED.decision, decision_reason = EXCLUDED.decision_reason,
                     approval_id = EXCLUDED.approval_id, status = EXCLUDED.status, updated_at = now()"""
            ),
            {
                "id": execution_id,
                "org": req.org_id,
                "wf": req.workflow_id,
                "run": req.agent_run_id,
                "tool": req.tool_name,
                "level": level,
                "decision": decision.decision.value,
                "reason": decision.reason,
                "approval": UUID(str(approval_id)) if approval_id else None,
                "key": req.idempotency_key,
                "digest": decision.request_digest,
                "status": status,
            },
        )

    async def _finish(
        self,
        conn: AsyncConnection,
        req: ToolRequest,
        execution_id: UUID,
        status: str,
        reason: str,
        result: dict[str, Any] | None,
        level: int,
    ) -> None:
        await conn.execute(
            text(
                """UPDATE tool_executions
               SET status = :status, decision_reason = :reason, result = CAST(:result AS jsonb),
                   finished_at = now(), updated_at = now()
               WHERE id = :id"""
            ),
            {
                "status": status,
                "reason": reason,
                "id": execution_id,
                "result": json.dumps(result, default=str) if result is not None else None,
            },
        )
        await self._audit(conn, req, execution_id, f"tool.{status}", reason)

    async def _audit(
        self,
        conn: AsyncConnection,
        req: ToolRequest,
        execution_id: UUID,
        action: str,
        reason: str,
        approval_id: Any = None,
    ) -> None:
        payload: dict[str, Any] = {"tool": req.tool_name, "reason": reason, "call_id": req.call_id}
        if approval_id:
            payload["approval_id"] = str(approval_id)
        await repo.record_audit(
            conn,
            org_id=req.org_id,
            actor_type="agent",
            actor_id=str(req.agent_run_id),
            action=action,
            target_type="tool_execution",
            target_id=str(execution_id),
            payload=payload,
        )
        log.info(action, tool=req.tool_name, execution_id=str(execution_id))
