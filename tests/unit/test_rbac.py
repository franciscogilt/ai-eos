from __future__ import annotations

import pytest

from aieos.api.rbac import ROLE_PERMISSIONS, Permission, require
from aieos.core.errors import ForbiddenError

P = Permission
READ = {P.ORG_READ, P.PROJECT_READ, P.REPOSITORY_READ, P.WORKFLOW_READ, P.APPROVAL_READ}
MANAGE = {P.PROJECT_WRITE, P.PROJECT_DELETE, P.REPOSITORY_WRITE, P.WORKFLOW_CREATE, P.WORKFLOW_CONTROL}
EXPECTED = {
    "viewer": READ,
    "member": READ | {P.WORKFLOW_CREATE},
    "approver": READ | {P.APPROVAL_DECIDE},
    "admin": READ | MANAGE,
    "owner": READ | MANAGE | {P.APPROVAL_DECIDE},
}


@pytest.mark.parametrize("role", list(EXPECTED))
@pytest.mark.parametrize("perm", list(Permission))
def test_matrix(role, perm):
    if perm in EXPECTED[role]:
        require(role, perm)
    else:
        with pytest.raises(ForbiddenError):
            require(role, perm)


def test_admins_cannot_approve_by_default():
    # Separation of duties: managing the platform and approving its changes are different roles.
    assert P.APPROVAL_DECIDE not in ROLE_PERMISSIONS["admin"]


def test_unknown_role_has_nothing():
    with pytest.raises(ForbiddenError):
        require("superuser", P.ORG_READ)
