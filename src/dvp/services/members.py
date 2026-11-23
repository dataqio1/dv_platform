"""MembershipService — manage who belongs to a group and what role they hold.

The RBAC model (roles → permissions) already exists in core.tenancy; this is the
missing admin surface to *assign* those roles. Writes are gated on MANAGE_GROUP, fully
audited, and guarded so an admin can't lock the group out of itself.

Mode-aware: when a group is bound to an LDAP group (`Group.ldap_group`), membership is
JIT-provisioned from the directory — this service treats it as **read-only** and refuses
writes with a clear message rather than letting the app and the directory drift.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from dvp.core.tenancy import Permission, Role, TenantContext
from dvp.db.models import Group, Membership, User
from dvp.db.session import MetadataStore
from dvp.services import audit

# Roles an admin may assign within a group. ORG_ADMIN is org-level — only an actor who
# already holds MANAGE_ORG may grant it.
_ASSIGNABLE_IN_GROUP = (Role.REPORT_VIEWER, Role.VIEWER, Role.EDITOR, Role.GROUP_ADMIN)


class MembershipService:
    def __init__(self, store: MetadataStore, settings=None) -> None:  # noqa: ANN001
        self.store = store
        self.settings = settings

    # ----------------------------- read -----------------------------
    def list(self, ctx: TenantContext) -> list[dict]:
        # Viewing the member roster is an administrative action (reveals who has access).
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            ldap = self._ldap_managed(s, ctx.group_id)
            rows = s.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.group_id == ctx.group_id)
                .order_by(User.email)
            ).all()
            return [_member_dict(m, u, ldap) for (m, u) in rows]

    # ----------------------------- write -----------------------------
    def add(self, ctx: TenantContext, *, email: str, role: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        role_enum = self._validate_role(ctx, role)
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("a valid email is required", details={"email": email})
        with self.store.session(ctx) as s:
            self._assert_writable(s, ctx)
            user = s.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, display_name=email.split("@")[0])
                s.add(user)
                s.flush()
            if s.scalar(
                select(Membership).where(
                    Membership.group_id == ctx.group_id, Membership.user_id == user.id
                )
            ):
                raise ValidationError("user is already a member", details={"email": email})
            m = Membership(group_id=ctx.group_id, user_id=user.id, role=role_enum.value)
            s.add(m)
            s.flush()
            audit.record(
                s, ctx, "member.add", entity_type="membership", entity_id=m.id,
                detail={"email": email, "role": role_enum.value},
            )
            return _member_dict(m, user, False)

    def update_role(self, ctx: TenantContext, user_id: str, role: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        role_enum = self._validate_role(ctx, role)
        with self.store.session(ctx) as s:
            self._assert_writable(s, ctx)
            m = self._load(s, ctx, user_id)
            if m.role == Role.GROUP_ADMIN.value and role_enum != Role.GROUP_ADMIN:
                self._assert_not_last_admin(s, ctx, user_id)
            m.role = role_enum.value
            s.flush()
            user = s.get(User, user_id)
            audit.record(
                s, ctx, "member.role_change", entity_type="membership", entity_id=m.id,
                detail={"user_id": user_id, "role": role_enum.value},
            )
            return _member_dict(m, user, False)

    def remove(self, ctx: TenantContext, user_id: str) -> None:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            self._assert_writable(s, ctx)
            m = self._load(s, ctx, user_id)
            if m.role == Role.GROUP_ADMIN.value:
                self._assert_not_last_admin(s, ctx, user_id)
            audit.record(
                s, ctx, "member.remove", entity_type="membership", entity_id=m.id,
                detail={"user_id": user_id},
            )
            s.delete(m)

    # ----------------------------- helpers -----------------------------
    def _load(self, s, ctx: TenantContext, user_id: str) -> Membership:  # noqa: ANN001
        m = s.scalar(
            select(Membership).where(
                Membership.group_id == ctx.group_id, Membership.user_id == user_id
            )
        )
        if m is None:
            raise NotFoundError("membership not found", details={"user_id": user_id})
        return m

    def _validate_role(self, ctx: TenantContext, role: str) -> Role:
        try:
            r = Role(role)
        except ValueError as exc:
            raise ValidationError(
                "unknown role",
                details={"role": role, "allowed": [x.value for x in _ASSIGNABLE_IN_GROUP]},
            ) from exc
        if r == Role.ORG_ADMIN:
            if not ctx.has(Permission.MANAGE_ORG):
                raise PermissionDeniedError("only an org admin may assign the org_admin role")
        elif r not in _ASSIGNABLE_IN_GROUP:
            raise ValidationError("role is not assignable within a group", details={"role": role})
        return r

    def _ldap_managed(self, s, group_id: str) -> bool:  # noqa: ANN001
        grp = s.get(Group, group_id)
        return bool(grp and grp.ldap_group)

    def _assert_writable(self, s, ctx: TenantContext) -> None:  # noqa: ANN001
        if self._ldap_managed(s, ctx.group_id):
            raise ValidationError(
                "membership for this group is managed by LDAP and is read-only here"
            )

    def _assert_not_last_admin(self, s, ctx: TenantContext, user_id: str) -> None:  # noqa: ANN001
        others = s.scalars(
            select(Membership).where(
                Membership.group_id == ctx.group_id,
                Membership.role == Role.GROUP_ADMIN.value,
                Membership.user_id != user_id,
            )
        ).all()
        if not others:
            raise ValidationError("cannot remove or demote the last group admin")


def _member_dict(m: Membership, u: User | None, ldap_managed: bool) -> dict:
    return {
        "user_id": m.user_id,
        "membership_id": m.id,
        "email": u.email if u else None,
        "display_name": u.display_name if u else None,
        "role": m.role,
        "ldap_managed": ldap_managed,
        "joined_at": m.created_at.isoformat() if m.created_at else None,
    }
