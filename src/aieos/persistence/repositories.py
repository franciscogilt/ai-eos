"""SQL for tenancy entities. Explicit SQL keeps RLS behaviour easy to audit.

All functions take an open connection; transaction boundaries belong to the caller.
Tenant-table functions must be called inside `Database.tenant()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aieos.core.errors import ConflictError, NotFoundError
from aieos.core.ids import uuid7


def integrity_kind(exc: IntegrityError) -> str | None:
    """Map a PostgreSQL integrity error to unique | foreign_key | check."""
    sqlstate = getattr(exc.orig, "sqlstate", None)
    return {"23505": "unique", "23503": "foreign_key", "23514": "check"}.get(sqlstate or "")


# ---------------------------------------------------------------------------- users (global)
@dataclass(frozen=True, slots=True)
class UserRow:
    id: UUID
    idp_subject: str
    email: str
    status: str


async def get_or_create_user(conn: AsyncConnection, *, sub: str, email: str, display_name: str | None) -> UserRow:
    row = (
        await conn.execute(
            text(
                """
                WITH ins AS (
                    INSERT INTO users (id, idp_subject, email, display_name)
                    VALUES (:id, :sub, :email, :display_name)
                    ON CONFLICT (idp_subject) DO NOTHING
                    RETURNING id, idp_subject, email, status
                )
                SELECT id, idp_subject, email, status FROM ins
                UNION ALL
                SELECT id, idp_subject, email, status FROM users WHERE idp_subject = :sub
                LIMIT 1
                """
            ),
            {"id": uuid7(), "sub": sub, "email": email, "display_name": display_name},
        )
    ).one()
    return UserRow(row.id, row.idp_subject, row.email, row.status)


# ---------------------------------------------------------------------------- organizations
@dataclass(frozen=True, slots=True)
class OrgRow:
    id: UUID
    slug: str
    name: str
    status: str


async def get_current_org(conn: AsyncConnection) -> OrgRow | None:
    """The organization named by app.org_id (RLS shows at most one row)."""
    row = (await conn.execute(text("SELECT id, slug, name, status FROM organizations"))).first()
    return OrgRow(row.id, row.slug, row.name, row.status) if row else None


async def get_membership_role(conn: AsyncConnection, user_id: UUID) -> str | None:
    row = (await conn.execute(text("SELECT role FROM memberships WHERE user_id = :u"), {"u": user_id})).first()
    return str(row.role) if row else None


# ---------------------------------------------------------------------------- projects
@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: UUID
    org_id: UUID
    slug: str
    name: str
    policy: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


_PROJECT_COLS = "id, org_id, slug, name, policy, version, created_at, updated_at"


def _project(row: Any) -> ProjectRow:
    return ProjectRow(
        row.id, row.org_id, row.slug, row.name, dict(row.policy), row.version, row.created_at, row.updated_at
    )


async def list_projects(conn: AsyncConnection, *, limit: int, offset: int) -> list[ProjectRow]:
    rows = await conn.execute(
        text(f"SELECT {_PROJECT_COLS} FROM projects ORDER BY created_at, id LIMIT :limit OFFSET :offset"),
        {"limit": limit, "offset": offset},
    )
    return [_project(r) for r in rows]


async def get_project(conn: AsyncConnection, project_id: UUID) -> ProjectRow:
    row = (await conn.execute(text(f"SELECT {_PROJECT_COLS} FROM projects WHERE id = :id"), {"id": project_id})).first()
    if row is None:
        raise NotFoundError("project not found")
    return _project(row)


async def create_project(
    conn: AsyncConnection, *, org_id: UUID, slug: str, name: str, policy: dict[str, Any]
) -> ProjectRow:
    try:
        row = (
            await conn.execute(
                text(
                    f"""INSERT INTO projects (id, org_id, slug, name, policy)
                        VALUES (:id, :org_id, :slug, :name, CAST(:policy AS jsonb))
                        RETURNING {_PROJECT_COLS}"""
                ),
                {"id": uuid7(), "org_id": org_id, "slug": slug, "name": name, "policy": json.dumps(policy)},
            )
        ).one()
    except IntegrityError as exc:
        if integrity_kind(exc) == "unique":
            raise ConflictError(f"project slug {slug!r} already exists") from None
        raise
    return _project(row)


async def update_project(
    conn: AsyncConnection,
    project_id: UUID,
    *,
    expected_version: int,
    name: str | None,
    policy: dict[str, Any] | None,
) -> ProjectRow:
    """Optimistic concurrency: succeeds only if the caller saw the current version."""
    row = (
        await conn.execute(
            text(
                f"""UPDATE projects
                    SET name = coalesce(:name, name),
                        policy = coalesce(CAST(:policy AS jsonb), policy),
                        version = version + 1,
                        updated_at = now()
                    WHERE id = :id AND version = :version
                    RETURNING {_PROJECT_COLS}"""
            ),
            {
                "id": project_id,
                "version": expected_version,
                "name": name,
                "policy": json.dumps(policy) if policy is not None else None,
            },
        )
    ).first()
    if row is None:
        current = await get_project(conn, project_id)  # raises NotFoundError if it does not exist
        raise ConflictError(f"version conflict: expected {expected_version}, current is {current.version}")
    return _project(row)


async def delete_project(conn: AsyncConnection, project_id: UUID) -> None:
    try:
        result = await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
    except IntegrityError as exc:
        if integrity_kind(exc) == "foreign_key":
            raise ConflictError("project has workflows; it cannot be deleted") from None
        raise
    if result.rowcount == 0:
        raise NotFoundError("project not found")


# ---------------------------------------------------------------------------- repositories
@dataclass(frozen=True, slots=True)
class RepositoryRow:
    id: UUID
    project_id: UUID
    provider: str
    external_id: int
    owner_login: str
    name: str
    default_branch: str
    github_installation_id: int
    status: str
    created_at: datetime


_REPO_COLS = (
    "id, project_id, provider, external_id, owner_login, name, default_branch, github_installation_id, status, "
    "created_at"
)


def _repository(row: Any) -> RepositoryRow:
    return RepositoryRow(
        row.id,
        row.project_id,
        row.provider,
        row.external_id,
        row.owner_login,
        row.name,
        row.default_branch,
        row.github_installation_id,
        row.status,
        row.created_at,
    )


async def list_repositories(conn: AsyncConnection, project_id: UUID) -> list[RepositoryRow]:
    rows = await conn.execute(
        text(f"SELECT {_REPO_COLS} FROM repositories WHERE project_id = :p ORDER BY created_at, id"),
        {"p": project_id},
    )
    return [_repository(r) for r in rows]


async def create_repository(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    project_id: UUID,
    external_id: int,
    owner_login: str,
    name: str,
    default_branch: str,
    github_installation_id: int,
) -> RepositoryRow:
    await get_project(conn, project_id)  # 404 before attempting the insert
    try:
        row = (
            await conn.execute(
                text(
                    f"""INSERT INTO repositories (id, org_id, project_id, provider, external_id, owner_login, name,
                                                  default_branch, github_installation_id)
                        VALUES (:id, :org_id, :project_id, 'github', :external_id, :owner_login, :name,
                                :default_branch, :installation)
                        RETURNING {_REPO_COLS}"""
                ),
                {
                    "id": uuid7(),
                    "org_id": org_id,
                    "project_id": project_id,
                    "external_id": external_id,
                    "owner_login": owner_login,
                    "name": name,
                    "default_branch": default_branch,
                    "installation": github_installation_id,
                },
            )
        ).one()
    except IntegrityError as exc:
        if integrity_kind(exc) == "unique":
            raise ConflictError("repository is already registered in this organization") from None
        raise
    return _repository(row)


# ---------------------------------------------------------------------------- audit
async def record_audit(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    actor_type: str,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append to the tenant's hash-chained audit log in the caller's transaction.
    Payloads must be secret-free; they are stored as given."""
    await conn.execute(
        text(
            """INSERT INTO audit_events (org_id, actor_type, actor_id, action, target_type, target_id, payload)
               VALUES (:org_id, :actor_type, :actor_id, :action, :target_type, :target_id, CAST(:payload AS jsonb))"""
        ),
        {
            "org_id": org_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "payload": json.dumps(payload or {}),
        },
    )


# ---------------------------------------------------------------------------- artifacts
@dataclass(frozen=True, slots=True)
class ArtifactRow:
    id: UUID
    org_id: UUID
    workflow_id: UUID | None
    kind: str
    s3_bucket: str
    s3_key: str
    sha256: str
    size_bytes: int
    content_type: str
    classification: str
    retention_class: str
    created_at: datetime


_ARTIFACT_COLS = (
    "id, org_id, workflow_id, kind, s3_bucket, s3_key, sha256, size_bytes, content_type, classification, "
    "retention_class, created_at"
)


def _artifact(row: Any) -> ArtifactRow:
    return ArtifactRow(
        row.id,
        row.org_id,
        row.workflow_id,
        row.kind,
        row.s3_bucket,
        row.s3_key,
        row.sha256,
        row.size_bytes,
        row.content_type,
        row.classification,
        row.retention_class,
        row.created_at,
    )


async def upsert_artifact(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    workflow_id: UUID | None,
    kind: str,
    s3_bucket: str,
    s3_key: str,
    sha256: str,
    size_bytes: int,
    content_type: str,
    classification: str,
    retention_class: str,
) -> ArtifactRow:
    """Idempotent: the same object key always maps to one row."""
    row = (
        await conn.execute(
            text(
                f"""WITH ins AS (
                        INSERT INTO artifacts (id, org_id, workflow_id, kind, s3_bucket, s3_key, sha256, size_bytes,
                                               content_type, classification, retention_class)
                        VALUES (:id, :org_id, :workflow_id, :kind, :bucket, :key, :sha, :size, :ctype, :cls, :ret)
                        ON CONFLICT (s3_bucket, s3_key) DO NOTHING
                        RETURNING {_ARTIFACT_COLS})
                    SELECT {_ARTIFACT_COLS} FROM ins
                    UNION ALL
                    SELECT {_ARTIFACT_COLS} FROM artifacts WHERE s3_bucket = :bucket AND s3_key = :key
                    LIMIT 1"""
            ),
            {
                "id": uuid7(),
                "org_id": org_id,
                "workflow_id": workflow_id,
                "kind": kind,
                "bucket": s3_bucket,
                "key": s3_key,
                "sha": sha256,
                "size": size_bytes,
                "ctype": content_type,
                "cls": classification,
                "ret": retention_class,
            },
        )
    ).first()
    if row is None:  # key exists but is invisible under RLS: never expected, since keys embed the org id
        raise ConflictError("artifact key belongs to another scope")
    return _artifact(row)


async def get_artifact(conn: AsyncConnection, artifact_id: UUID) -> ArtifactRow:
    row = (
        await conn.execute(text(f"SELECT {_ARTIFACT_COLS} FROM artifacts WHERE id = :id"), {"id": artifact_id})
    ).first()
    if row is None:
        raise NotFoundError("artifact not found")
    return _artifact(row)
