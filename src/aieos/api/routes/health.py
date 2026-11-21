"""Liveness and readiness probes (unauthenticated, no tenant data)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from aieos.api.deps import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    if await get_db(request).ping():
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "unavailable"}
