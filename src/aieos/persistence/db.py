"""Async database access with tenant-scoped transactions.

Every tenant query runs inside `Database.tenant(org_id)`, which sets
`app.org_id` with `set_config(..., true)`: transaction-local, so the value is
discarded on COMMIT/ROLLBACK and can never leak to the next user of a pooled
connection. Row-level security does the filtering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from aieos.core.logging import get_logger

log = get_logger(__name__)


class RuntimeRoleError(RuntimeError):
    """The runtime database role could bypass tenant isolation."""


class Database:
    def __init__(self, url: str, *, pool_size: int = 5, max_overflow: int = 5, statement_timeout_ms: int = 15_000):
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            connect_args={"options": f"-c statement_timeout={int(statement_timeout_ms)}"},
        )

    @asynccontextmanager
    async def tenant(self, org_id: UUID) -> AsyncIterator[AsyncConnection]:
        """One transaction scoped to one organization. Commits on success, rolls back on error."""
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)})
            yield conn

    @asynccontextmanager
    async def global_tx(self) -> AsyncIterator[AsyncConnection]:
        """Transaction without tenant context. Tenant tables return no rows here (RLS);
        use only for global tables such as `users`."""
        async with self.engine.begin() as conn:
            yield conn

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - readiness must report, not raise
            log.warning("db.ping_failed")
            return False

    async def assert_runtime_role_is_safe(self) -> None:
        """Refuse to serve if the runtime role owns the schema or bypasses RLS."""
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT current_user AS role_name,
                               r.rolsuper OR r.rolbypassrls AS privileged,
                               EXISTS (SELECT 1 FROM pg_tables
                                       WHERE schemaname = 'public' AND tablename = 'organizations'
                                         AND tableowner = current_user) AS owns_tables,
                               pg_has_role(current_user, 'aieos_app', 'MEMBER') AS is_app_role
                        FROM pg_roles r WHERE r.rolname = current_user
                        """
                    )
                )
            ).one()
        if row.privileged or row.owns_tables or not row.is_app_role:
            raise RuntimeRoleError(
                f"database role {row.role_name!r} is unsafe for runtime use "
                f"(privileged={row.privileged}, owns_tables={row.owns_tables}, is_app_role={row.is_app_role}); "
                "connect as a member of aieos_app"
            )
        log.info("db.runtime_role_ok", role=row.role_name)

    async def dispose(self) -> None:
        await self.engine.dispose()
