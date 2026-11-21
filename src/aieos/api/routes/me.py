"""The authenticated user."""

from __future__ import annotations

from fastapi import APIRouter

from aieos.api.deps import CurrentPrincipal
from aieos.api.schemas import MeOut

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me")
async def get_me(principal: CurrentPrincipal) -> MeOut:
    return MeOut(id=principal.user_id, email=principal.email, subject=principal.sub)
