"""Request dependencies: authentication and organization authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.concurrency import run_in_threadpool

from aieos.api.auth import TokenVerifier
from aieos.api.rbac import Permission, require
from aieos.core.errors import AuthenticationError, ForbiddenError, NotFoundError
from aieos.persistence import repositories as repo
from aieos.persistence.db import Database


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    sub: str
    email: str


@dataclass(frozen=True, slots=True)
class OrgContext:
    org: repo.OrgRow
    role: str
    principal: Principal


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("missing bearer token")
    return token.strip()


async def current_principal(request: Request) -> Principal:
    verifier: TokenVerifier = request.app.state.verifier
    # JWKS fetches are blocking I/O; keep them off the event loop.
    verified = await run_in_threadpool(verifier.verify, _bearer_token(request))
    email = verified.email or f"{verified.sub}@users.invalid"
    async with get_db(request).global_tx() as conn:
        user = await repo.get_or_create_user(conn, sub=verified.sub, email=email, display_name=verified.name)
    if user.status != "active":
        raise ForbiddenError("user is disabled")
    return Principal(user_id=user.id, sub=user.idp_subject, email=user.email)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def authorize_org(conn: AsyncConnection, principal: Principal, permission: Permission) -> OrgContext:
    """Call first inside `db.tenant(org_id)`.

    Non-members get 404, so organization ids cannot be probed. Members without the
    permission get 403.
    """
    org = await repo.get_current_org(conn)
    role = await repo.get_membership_role(conn, principal.user_id) if org else None
    if org is None or role is None or org.status != "active":
        raise NotFoundError("organization not found")
    require(role, permission)
    return OrgContext(org=org, role=role, principal=principal)
