"""Organization roles and the permissions they grant.

Deciding an approval additionally requires the role to be listed in the project's
policy (`approver_roles`), and Level 4 decisions follow the four-eyes rule.
"""

from __future__ import annotations

from enum import StrEnum

from aieos.core.errors import ForbiddenError


class Permission(StrEnum):
    ORG_READ = "org.read"
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    PROJECT_DELETE = "project.delete"
    REPOSITORY_READ = "repository.read"
    REPOSITORY_WRITE = "repository.write"
    WORKFLOW_READ = "workflow.read"
    WORKFLOW_CREATE = "workflow.create"
    WORKFLOW_CONTROL = "workflow.control"  # cancel / answer clarifications of any workflow
    APPROVAL_READ = "approval.read"
    APPROVAL_DECIDE = "approval.decide"


_READ = frozenset(
    {
        Permission.ORG_READ,
        Permission.PROJECT_READ,
        Permission.REPOSITORY_READ,
        Permission.WORKFLOW_READ,
        Permission.APPROVAL_READ,
    }
)
_MANAGE = _READ | {
    Permission.PROJECT_WRITE,
    Permission.PROJECT_DELETE,
    Permission.REPOSITORY_WRITE,
    Permission.WORKFLOW_CREATE,
    Permission.WORKFLOW_CONTROL,
}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": _READ,
    "member": _READ | {Permission.WORKFLOW_CREATE},  # may control only workflows they created
    "approver": _READ | {Permission.APPROVAL_DECIDE},
    "admin": _MANAGE,
    "owner": _MANAGE | {Permission.APPROVAL_DECIDE},
}


def require(role: str, permission: Permission) -> None:
    if permission not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise ForbiddenError(f"role {role!r} lacks {permission.value}")
