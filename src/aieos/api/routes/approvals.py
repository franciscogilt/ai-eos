"""Human approvals. The decision is committed to the database first, then signalled to the
workflow. A lost signal is recovered by the workflow's polling backstop."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from aieos.api.deps import CurrentPrincipal, authorize_org, get_db
from aieos.api.rbac import Permission
from aieos.api.schemas import ApprovalDecision, ApprovalOut
from aieos.core.errors import ConflictError, ForbiddenError, NotFoundError
from aieos.core.logging import get_logger
from aieos.gateway.policy import ProjectPolicy
from aieos.orchestration.models import ApprovalDecisionSignal
from aieos.persistence import repositories as repo

log = get_logger(__name__)
router = APIRouter(prefix="/v1/orgs/{org_id}/approvals", tags=["approvals"])

_COLS = (
    "a.id, a.workflow_id, a.kind, a.permission_level, a.tool_name, a.summary, a.status, a.decided_by_user_id, "
    "a.decided_at, a.decision_comment, a.expires_at, a.version, a.created_at"
)


def _out(row: Any, **extra: Any) -> ApprovalOut:
    data = {k: getattr(row, k) for k in ApprovalOut.model_fields if k != "signal_delivered"}
    return ApprovalOut(**data, **extra)


@router.get("")
async def list_approvals(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    status_filter: Annotated[str, Query(alias="status", pattern=r"^[a-z]+$")] = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ApprovalOut]:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.APPROVAL_READ)
        rows = await conn.execute(
            text(f"SELECT {_COLS} FROM approvals a WHERE a.status = :s ORDER BY a.created_at LIMIT :limit"),
            {"s": status_filter, "limit": limit},
        )
        return [_out(r) for r in rows]


@router.get("/{approval_id}")
async def get_approval(org_id: UUID, approval_id: UUID, request: Request, principal: CurrentPrincipal) -> ApprovalOut:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.APPROVAL_READ)
        row = (
            await conn.execute(text(f"SELECT {_COLS} FROM approvals a WHERE a.id = :id"), {"id": approval_id})
        ).first()
    if row is None:
        raise NotFoundError("approval not found")
    return _out(row)


@router.post("/{approval_id}/decision")
async def decide(
    org_id: UUID, approval_id: UUID, body: ApprovalDecision, request: Request, principal: CurrentPrincipal
) -> ApprovalOut:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.APPROVAL_DECIDE)
        row = (
            await conn.execute(
                text(
                    f"""SELECT {_COLS}, a.requested_by_user_id, (a.expires_at <= now()) AS expired,
                       w.temporal_workflow_id, p.policy
                FROM approvals a JOIN workflows w ON w.id = a.workflow_id JOIN projects p ON p.id = w.project_id
                WHERE a.id = :id FOR UPDATE OF a"""
                ),
                {"id": approval_id},
            )
        ).first()
        if row is None:
            raise NotFoundError("approval not found")
        if row.status != "pending":
            raise ConflictError(f"approval is already {row.status}")
        if row.expired:
            raise ConflictError("approval has expired; the workflow will request a new one")
        if row.version != body.version:
            raise ConflictError(f"version conflict: expected {body.version}, current is {row.version}")
        policy = ProjectPolicy.model_validate(row.policy or {})
        if ctx.role not in policy.approver_roles:
            raise ForbiddenError(f"role {ctx.role!r} is not an approver for this project")
        if row.requested_by_user_id is not None and row.requested_by_user_id == principal.user_id:
            raise ForbiddenError("four-eyes rule: you requested this operation and cannot approve it")
        new_status = "approved" if body.decision == "approve" else "rejected"
        updated = (
            await conn.execute(
                text(
                    f"""UPDATE approvals a SET status = :s, decided_by_user_id = :by, decided_at = now(),
                       decision_comment = :c, version = version + 1
                WHERE a.id = :id RETURNING {_COLS}"""
                ),
                {"s": new_status, "by": principal.user_id, "c": body.comment, "id": approval_id},
            )
        ).one()
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action=f"approval.{new_status}",
            target_type="approval",
            target_id=str(approval_id),
            payload={"tool": row.tool_name, "level": row.permission_level, "comment": body.comment},
        )
    delivered = True
    try:
        await request.app.state.orchestrator.signal_approval(
            row.temporal_workflow_id, ApprovalDecisionSignal(approval_id=approval_id, decision=new_status)
        )
    except Exception:  # noqa: BLE001 - the decision is committed; the workflow's poll will pick it up
        delivered = False
        log.warning("approval.signal_failed", approval_id=str(approval_id))
    return _out(updated, signal_delivered=delivered)
