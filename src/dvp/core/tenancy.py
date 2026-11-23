"""Tenant context & RBAC primitives.

TenantContext is a required argument to every repository/service method — there is
no code path that reads tenant data without it (docs/design/02-multi-tenancy.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from dvp.core.errors import PermissionDeniedError


class Role(StrEnum):
    REPORT_VIEWER = "report_viewer"  # can view reports only — nothing else
    VIEWER = "viewer"  # read-only across the platform (incl. connections)
    EDITOR = "editor"  # create/run test cases & monitors; cannot configure connections
    GROUP_ADMIN = "group_admin"  # + configure connections, members, secrets
    ORG_ADMIN = "org_admin"


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"  # CRUD monitors/rules, trigger runs/compares
    VIEW_REPORTS = "view_reports"  # read reports (the report-only persona)
    MANAGE_CONNECTIONS = "manage_connections"  # create/edit/delete data source connections
    MANAGE_PROJECTS = "manage_projects"  # create/delete/restore projects (admins, not editors)
    MANAGE_GROUP = "manage_group"  # members, secrets, quotas
    MANAGE_ORG = "manage_org"  # create/delete groups, org policy


_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.REPORT_VIEWER: {Permission.VIEW_REPORTS},
    Role.VIEWER: {Permission.READ, Permission.VIEW_REPORTS},
    # editors work *within* projects (create/run test cases) but can't create/delete them
    Role.EDITOR: {Permission.READ, Permission.WRITE, Permission.VIEW_REPORTS},
    Role.GROUP_ADMIN: {
        Permission.READ,
        Permission.WRITE,
        Permission.VIEW_REPORTS,
        Permission.MANAGE_CONNECTIONS,
        Permission.MANAGE_PROJECTS,
        Permission.MANAGE_GROUP,
    },
    # org admins get data access within a group only via their group membership/role
    Role.ORG_ADMIN: {Permission.MANAGE_ORG, Permission.MANAGE_PROJECTS},
}


@dataclass(frozen=True)
class TenantContext:
    """Immutable per-request scope. Established once at the API boundary."""

    org_id: str
    group_id: str
    user_id: str
    role: Role
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    @classmethod
    def build(cls, org_id: str, group_id: str, user_id: str, role: Role) -> TenantContext:
        return cls(
            org_id=org_id,
            group_id=group_id,
            user_id=user_id,
            role=role,
            permissions=frozenset(_ROLE_PERMISSIONS.get(role, set())),
        )

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDeniedError(
                f"role '{self.role}' lacks permission '{permission}'",
                details={"required": permission, "role": self.role},
            )
