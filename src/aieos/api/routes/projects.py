"""Projects within an organization."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from aieos.api.deps import CurrentPrincipal, authorize_org, get_db
from aieos.api.rbac import Permission
from aieos.api.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from aieos.persistence import repositories as repo

router = APIRouter(prefix="/v1/orgs/{org_id}/projects", tags=["projects"])


@router.get("")
async def list_projects(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[ProjectOut]:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.PROJECT_READ)
        rows = await repo.list_projects(conn, limit=limit, offset=offset)
    return [ProjectOut.from_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    org_id: UUID, body: ProjectCreate, request: Request, principal: CurrentPrincipal
) -> ProjectOut:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.PROJECT_WRITE)
        row = await repo.create_project(
            conn, org_id=ctx.org.id, slug=body.slug, name=body.name, policy=body.policy.model_dump(mode="json")
        )
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="project.created",
            target_type="project",
            target_id=str(row.id),
            payload={"slug": row.slug},
        )
    return ProjectOut.from_row(row)


@router.get("/{project_id}")
async def get_project(org_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal) -> ProjectOut:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.PROJECT_READ)
        row = await repo.get_project(conn, project_id)
    return ProjectOut.from_row(row)


@router.patch("/{project_id}")
async def update_project(
    org_id: UUID, project_id: UUID, body: ProjectUpdate, request: Request, principal: CurrentPrincipal
) -> ProjectOut:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.PROJECT_WRITE)
        row = await repo.update_project(
            conn,
            project_id,
            expected_version=body.version,
            name=body.name,
            policy=body.policy.model_dump(mode="json") if body.policy is not None else None,
        )
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="project.updated",
            target_type="project",
            target_id=str(row.id),
            payload={"version": row.version, "fields": sorted(body.model_fields_set - {"version"})},
        )
    return ProjectOut.from_row(row)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(org_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal) -> Response:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.PROJECT_DELETE)
        await repo.delete_project(conn, project_id)
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="project.deleted",
            target_type="project",
            target_id=str(project_id),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
