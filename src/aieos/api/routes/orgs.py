"""Organizations. Creation is an operator action (`aieos-admin create-org`), not an API call."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from aieos.api.deps import CurrentPrincipal, authorize_org, get_db
from aieos.api.rbac import Permission
from aieos.api.schemas import OrgOut

router = APIRouter(prefix="/v1/orgs", tags=["organizations"])


@router.get("/{org_id}")
async def get_org(org_id: UUID, request: Request, principal: CurrentPrincipal) -> OrgOut:
    async with get_db(request).tenant(org_id) as conn:
        ctx = await authorize_org(conn, principal, Permission.ORG_READ)
    return OrgOut.from_row(ctx.org, ctx.role)
