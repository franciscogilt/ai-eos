"""The authenticated user."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from aieos.api.deps import CurrentPrincipal, get_db
from aieos.api.schemas import MeOut, MyOrgOut

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me")
async def get_me(principal: CurrentPrincipal) -> MeOut:
    return MeOut(id=principal.user_id, email=principal.email, subject=principal.sub)


@router.get("/me/orgs")
async def my_orgs(request: Request, principal: CurrentPrincipal) -> list[MyOrgOut]:
    """Organizations the signed-in user belongs to (only their own memberships; see migration 0007)."""
    async with get_db(request).engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT org_id, slug, name, role FROM app_user_orgs(:user)"), {"user": principal.user_id}
        )
        return [MyOrgOut(id=r.org_id, slug=r.slug, name=r.name, role=r.role) for r in rows]
