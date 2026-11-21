"""Feature-delivery workflows: create (idempotent), read, cancel, answer clarifications."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aieos.api.deps import CurrentPrincipal, OrgContext, authorize_org, get_db
from aieos.api.rbac import ROLE_PERMISSIONS, Permission
from aieos.api.schemas import CancelRequest, ClarificationRequest, WorkflowCreate, WorkflowOut
from aieos.core.errors import ConflictError, ForbiddenError, NotFoundError
from aieos.core.ids import uuid7
from aieos.orchestration.client import Orchestrator
from aieos.orchestration.models import ClarificationAnswers, StartDeliveryInput
from aieos.persistence import repositories as repo

router = APIRouter(prefix="/v1/orgs/{org_id}", tags=["workflows"])

_COLS = (
    "id, project_id, repository_id, kind, title, status, budget_usd, spent_usd, budget_tokens, spent_tokens, "
    "created_by, created_at, finished_at, temporal_workflow_id"
)


def _orchestrator(request: Request) -> Orchestrator:
    orch: Orchestrator = request.app.state.orchestrator
    return orch


def _out(row: Any) -> WorkflowOut:
    return WorkflowOut(**{k: getattr(row, k) for k in WorkflowOut.model_fields})


async def _get(conn: AsyncConnection, workflow_id: UUID) -> Any:
    row = (await conn.execute(text(f"SELECT {_COLS} FROM workflows WHERE id = :id"), {"id": workflow_id})).first()
    if row is None:
        raise NotFoundError("workflow not found")
    return row


def _require_control(ctx: OrgContext, row: Any) -> None:
    if Permission.WORKFLOW_CONTROL in ROLE_PERMISSIONS[ctx.role]:
        return
    if row.created_by == ctx.principal.user_id and Permission.WORKFLOW_CREATE in ROLE_PERMISSIONS[ctx.role]:
        return
    raise ForbiddenError("only the workflow's creator, an admin or an owner can do this")


@router.post("/projects/{project_id}/workflows", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    org_id: UUID,
    project_id: UUID,
    body: WorkflowCreate,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
) -> WorkflowOut:
    workflow_id = uuid7()
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.WORKFLOW_CREATE)
        await repo.get_project(conn, project_id)
        owns = (
            await conn.execute(
                text("SELECT 1 FROM repositories WHERE id = :r AND project_id = :p"),
                {"r": body.repository_id, "p": project_id},
            )
        ).first()
        if owns is None:
            raise NotFoundError("repository not found in this project")
        row = (
            await conn.execute(
                text(
                    f"""INSERT INTO workflows (id, org_id, project_id, repository_id, kind, title, temporal_workflow_id,
                                       idempotency_key, created_by, budget_usd, budget_tokens)
                VALUES (:id, :org, :p, :r, 'feature_delivery', :title, :tid, :key, :by, :usd, :tok)
                ON CONFLICT (org_id, idempotency_key) DO NOTHING
                RETURNING {_COLS}"""
                ),
                {
                    "id": workflow_id,
                    "org": ctx.org.id,
                    "p": project_id,
                    "r": body.repository_id,
                    "title": body.title,
                    "tid": f"feature-delivery-{workflow_id}",
                    "key": body.idempotency_key,
                    "by": principal.user_id,
                    "usd": body.budget_usd,
                    "tok": body.budget_tokens,
                },
            )
        ).first()
        if row is None:  # same idempotency key: return the existing workflow (and re-attempt its start)
            row = (
                await conn.execute(
                    text(f"SELECT {_COLS} FROM workflows WHERE idempotency_key = :k"), {"k": body.idempotency_key}
                )
            ).one()
            if row.project_id != project_id or row.repository_id != body.repository_id:
                raise ConflictError("idempotency key already used for a different workflow")
            response.status_code = status.HTTP_200_OK
        else:
            await repo.record_audit(
                conn,
                org_id=ctx.org.id,
                actor_type="user",
                actor_id=str(principal.user_id),
                action="workflow.created",
                target_type="workflow",
                target_id=str(row.id),
                payload={"title": body.title, "budget_usd": str(body.budget_usd)},
            )
    # Started after commit: if Temporal is down the row stays 'pending' and the client retries with the same key.
    if row.status == "pending":
        await _orchestrator(request).start_delivery(
            row.temporal_workflow_id,
            StartDeliveryInput(
                org_id=org_id,
                workflow_id=row.id,
                project_id=row.project_id,
                repository_id=row.repository_id,
                request=body.request,
                requested_by=principal.user_id,
            ),
        )
    return _out(row)


@router.get("/workflows")
async def list_workflows(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    status_filter: Annotated[str | None, Query(alias="status", pattern=r"^[a-z_]+$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[WorkflowOut]:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.WORKFLOW_READ)
        rows = await conn.execute(
            text(
                f"""SELECT {_COLS} FROM workflows WHERE (CAST(:s AS text) IS NULL OR status = :s)
                ORDER BY created_at DESC LIMIT :limit"""
            ),
            {"s": status_filter, "limit": limit},
        )
        return [_out(r) for r in rows]


@router.get("/workflows/{workflow_id}")
async def get_workflow(org_id: UUID, workflow_id: UUID, request: Request, principal: CurrentPrincipal) -> WorkflowOut:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.WORKFLOW_READ)
        return _out(await _get(conn, workflow_id))


@router.post("/workflows/{workflow_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_workflow(
    org_id: UUID, workflow_id: UUID, body: CancelRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, str]:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.WORKFLOW_READ)
        row = await _get(conn, workflow_id)
        _require_control(ctx, row)
        if row.status in ("succeeded", "failed", "cancelled"):
            raise ConflictError(f"workflow is already {row.status}")
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="workflow.cancel_requested",
            target_type="workflow",
            target_id=str(workflow_id),
            payload={"reason": body.reason},
        )
    await _orchestrator(request).request_cancel(row.temporal_workflow_id, body.reason)
    return {"status": "cancel_requested"}


@router.post("/workflows/{workflow_id}/clarifications", status_code=status.HTTP_202_ACCEPTED)
async def answer_clarifications(
    org_id: UUID, workflow_id: UUID, body: ClarificationRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, str]:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.WORKFLOW_READ)
        row = await _get(conn, workflow_id)
        _require_control(ctx, row)
        if row.status != "awaiting_input":
            raise ConflictError("workflow is not waiting for clarification")
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="workflow.clarified",
            target_type="workflow",
            target_id=str(workflow_id),
            payload={"question_ids": sorted(body.answers)},
        )
    await _orchestrator(request).signal_clarification(
        row.temporal_workflow_id, ClarificationAnswers(answers=body.answers)
    )
    return {"status": "delivered"}
