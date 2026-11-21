"""Repository registration (metadata only; GitHub access arrives with the GitHub App in B13)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status

from aieos.api.deps import CurrentPrincipal, authorize_org, get_db
from aieos.api.rbac import Permission
from aieos.api.schemas import RepositoryCreate, RepositoryOut
from aieos.persistence import repositories as repo

router = APIRouter(prefix="/v1/orgs/{org_id}/projects/{project_id}/repositories", tags=["repositories"])


@router.get("")
async def list_repositories(
    org_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[RepositoryOut]:
    async with get_db(request).tenant(org_id) as conn:
        await authorize_org(conn, principal, Permission.REPOSITORY_READ)
        await repo.get_project(conn, project_id)
        rows = await repo.list_repositories(conn, project_id)
    return [RepositoryOut.from_row(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_repository(
    org_id: UUID, project_id: UUID, body: RepositoryCreate, request: Request, principal: CurrentPrincipal
) -> RepositoryOut:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.REPOSITORY_WRITE)
        row = await repo.create_repository(
            conn,
            org_id=ctx.org.id,
            project_id=project_id,
            external_id=body.external_id,
            owner_login=body.owner_login,
            name=body.name,
            default_branch=body.default_branch,
            github_installation_id=body.github_installation_id,
        )
        await repo.record_audit(
            conn,
            org_id=ctx.org.id,
            actor_type="user",
            actor_id=str(principal.user_id),
            action="repository.registered",
            target_type="repository",
            target_id=str(row.id),
            payload={"full_name": f"{row.owner_login}/{row.name}", "external_id": row.external_id},
        )
    return RepositoryOut.from_row(row)
