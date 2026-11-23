"""Tenant/identity services: local bootstrap + membership→context resolution +
LDAP-group-driven tenant provisioning (each LDAP group is a tenant)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from dvp.abstractions.identity import Identity
from dvp.config import Settings
from dvp.core.errors import PermissionDeniedError
from dvp.core.tenancy import Role, TenantContext
from dvp.db.models import Group, Membership, Organization, User
from dvp.db.session import MetadataStore


@dataclass(frozen=True)
class LocalTenant:
    org_id: str
    group_id: str
    user_id: str


class GroupRoleMapper:
    """Maps LDAP groups → tenants + roles.

    Two kinds of LDAP group:
      * tenant groups define the data boundary (a tenant per group);
      * role groups grant a capability level (admin / editor / viewer / report-viewer) and
        are NOT tenants themselves.
    A user's role is the HIGHEST role conferred by the role groups they belong to, applied
    across their tenants (falling back to `default_role`). So `finance` makes someone a
    member of the finance tenant, while `dq-admins`/`dq-reports` decides what they can do."""

    # higher value = more capability; used to pick the strongest matching role group
    _RANK = {
        Role.REPORT_VIEWER: 0,
        Role.VIEWER: 1,
        Role.EDITOR: 2,
        Role.GROUP_ADMIN: 3,
        Role.ORG_ADMIN: 4,
    }

    def __init__(
        self,
        *,
        admin_groups: list[str] | None = None,
        editor_groups: list[str] | None = None,
        viewer_groups: list[str] | None = None,
        report_viewer_groups: list[str] | None = None,
        default_role: Role = Role.EDITOR,
        tenant_groups: list[str] | None = None,
    ) -> None:
        self._role_groups: dict[str, Role] = {}
        for g in report_viewer_groups or []:
            self._role_groups[g] = Role.REPORT_VIEWER
        for g in viewer_groups or []:
            self._role_groups[g] = Role.VIEWER
        for g in editor_groups or []:
            self._role_groups[g] = Role.EDITOR
        for g in admin_groups or []:
            self._role_groups[g] = Role.GROUP_ADMIN
        self.default_role = default_role
        self.tenant_groups = set(tenant_groups or [])  # empty = all non-role groups are tenants

    @classmethod
    def from_settings(cls, settings: Settings) -> GroupRoleMapper:
        return cls(
            admin_groups=settings.ldap_admin_groups,
            editor_groups=settings.ldap_editor_groups,
            viewer_groups=settings.ldap_viewer_groups,
            report_viewer_groups=settings.ldap_report_viewer_groups,
            default_role=Role(settings.ldap_default_role),
            tenant_groups=settings.ldap_tenant_groups,
        )

    def is_tenant(self, ldap_group: str) -> bool:
        if ldap_group in self._role_groups:
            return False  # a role group is never a tenant
        return not self.tenant_groups or ldap_group in self.tenant_groups

    def role_for(self, ldap_groups: list[str]) -> Role:
        """The strongest role granted by the user's role groups (else the default)."""
        roles = [self._role_groups[g] for g in ldap_groups if g in self._role_groups]
        if not roles:
            return self.default_role
        return max(roles, key=lambda r: self._RANK[r])


class TenantProvisioner:
    """JIT-provisions tenants from a user's LDAP groups: ensures the user, a tenant
    (Organization + Group) per mapped LDAP group, and the membership with the mapped role.
    Memberships to LDAP-managed groups the user has left are revoked, so directory changes
    take effect on next login."""

    def __init__(self, store: MetadataStore, mapper: GroupRoleMapper) -> None:
        self.store = store
        self.mapper = mapper

    def sync(self, identity: Identity) -> list[dict]:
        tenant_groups = [g for g in identity.groups if self.mapper.is_tenant(g)]
        role = self.mapper.role_for(list(identity.groups))  # role groups confer the level
        with self.store.session() as s:
            user = s.scalar(select(User).where(User.email == identity.email))
            if user is None:
                user = User(
                    email=identity.email,
                    display_name=identity.display_name,
                    external_idp_sub=identity.external_sub,
                )
                s.add(user)
                s.flush()
            elif identity.display_name and user.display_name != identity.display_name:
                user.display_name = identity.display_name

            accessible: list[dict] = []
            wanted: set[str] = set()
            for lg in tenant_groups:
                group = s.scalar(select(Group).where(Group.ldap_group == lg))
                if group is None:
                    org = Organization(name=lg)
                    s.add(org)
                    s.flush()
                    group = Group(org_id=org.id, name=lg, ldap_group=lg)
                    s.add(group)
                    s.flush()
                wanted.add(group.id)
                m = s.scalar(
                    select(Membership).where(
                        Membership.user_id == user.id, Membership.group_id == group.id
                    )
                )
                if m is None:
                    s.add(Membership(group_id=group.id, user_id=user.id, role=role.value))
                elif m.role != role.value:
                    m.role = role.value
                accessible.append(
                    {
                        "user_id": user.id,
                        "group_id": group.id,
                        "org_id": group.org_id,
                        "name": group.name,
                        "ldap_group": lg,
                        "role": role.value,
                    }
                )

            # revoke LDAP-managed memberships the user no longer qualifies for
            mine = s.scalars(select(Membership).where(Membership.user_id == user.id)).all()
            for m in mine:
                g = s.get(Group, m.group_id)
                if g and g.ldap_group is not None and g.id not in wanted:
                    s.delete(m)
            return accessible


def bootstrap_local_tenant(store: MetadataStore, settings: Settings) -> LocalTenant:
    """Provision the single org/group/user for desktop mode (D2). Idempotent."""
    with store.session() as s:
        user = s.scalar(select(User).where(User.email == settings.local_user_email))
        if user is None:
            org = Organization(name=settings.local_org_name)
            s.add(org)
            s.flush()
            group = Group(org_id=org.id, name=settings.local_group_name)
            s.add(group)
            s.flush()
            user = User(email=settings.local_user_email, display_name="Local User")
            s.add(user)
            s.flush()
            s.add(Membership(group_id=group.id, user_id=user.id, role=Role.GROUP_ADMIN.value))
            s.flush()
            return LocalTenant(org_id=org.id, group_id=group.id, user_id=user.id)

        membership = s.scalar(select(Membership).where(Membership.user_id == user.id))
        group = s.get(Group, membership.group_id)
        return LocalTenant(org_id=group.org_id, group_id=group.id, user_id=user.id)


def resolve_context(
    store: MetadataStore, *, user_id: str, org_id: str, group_id: str
) -> TenantContext:
    """Build a TenantContext, asserting the user is a member of the group."""
    with store.session() as s:
        membership = s.scalar(
            select(Membership).where(Membership.user_id == user_id, Membership.group_id == group_id)
        )
        if membership is None:
            raise PermissionDeniedError(
                "user is not a member of the requested group",
                details={"group_id": group_id},
            )
        return TenantContext.build(
            org_id=org_id, group_id=group_id, user_id=user_id, role=Role(membership.role)
        )
