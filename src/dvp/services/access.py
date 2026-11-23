"""AccessService — the read-only RBAC matrix plus service-account (API key) management.

Powers the "People & access" admin screen: it exposes the static role→permission matrix the
UI renders, and manages non-human identities. API keys follow the store-only-a-hash rule —
the plaintext key is returned exactly once, at creation/rotation; after that only its prefix
is ever shown. Secrets never hit the DB in the clear (same discipline as connection secrets).
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import _ROLE_PERMISSIONS, Permission, Role, TenantContext
from dvp.db.models import ServiceAccount
from dvp.db.session import MetadataStore
from dvp.services import audit

# Roles assignable to a service account (never ORG_ADMIN — org policy is a human concern).
_ASSIGNABLE = (Role.REPORT_VIEWER, Role.VIEWER, Role.EDITOR, Role.GROUP_ADMIN)


def role_matrix() -> dict:
    """Static role→permission matrix for the UI. Derived from the single source of truth in
    core.tenancy so the screen can never drift from what the server actually enforces."""
    permissions = [p.value for p in Permission]
    roles = []
    for role in Role:
        granted = {p.value for p in _ROLE_PERMISSIONS.get(role, set())}
        roles.append(
            {
                "role": role.value,
                "permissions": sorted(granted),
                "matrix": {p: (p in granted) for p in permissions},
            }
        )
    return {"permissions": permissions, "roles": roles}


class AccessService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def list_service_accounts(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(ServiceAccount)
                .where(ServiceAccount.group_id == ctx.group_id)
                .order_by(ServiceAccount.created_at.desc())
            ).all()
            return [_sa_dict(r) for r in rows]

    def create_service_account(self, ctx: TenantContext, *, name: str, role: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        name = (name or "").strip()
        if not name:
            raise ValidationError("service account name is required")
        role_enum = self._validate_role(role)
        with self.store.session(ctx) as s:
            if s.scalar(
                select(ServiceAccount).where(
                    ServiceAccount.group_id == ctx.group_id, ServiceAccount.name == name
                )
            ):
                raise ValidationError("a service account with this name already exists",
                                      details={"name": name})
            prefix, secret, key_hash = _mint_key()
            sa = ServiceAccount(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                name=name,
                role=role_enum.value,
                key_prefix=prefix,
                key_hash=key_hash,
                active=True,
                created_by=ctx.user_id,
            )
            s.add(sa)
            s.flush()
            audit.record(
                s, ctx, "service_account.create", entity_type="service_account", entity_id=sa.id,
                detail={"name": name, "role": role_enum.value},
            )
            out = _sa_dict(sa)
        # The full key is returned exactly once and never stored in the clear.
        out["api_key"] = secret
        return out

    def rotate_key(self, ctx: TenantContext, service_account_id: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            sa = self._load(s, ctx, service_account_id)
            prefix, secret, key_hash = _mint_key()
            sa.key_prefix = prefix
            sa.key_hash = key_hash
            sa.active = True
            audit.record(
                s, ctx, "service_account.rotate", entity_type="service_account", entity_id=sa.id,
            )
            out = _sa_dict(sa)
        out["api_key"] = secret
        return out

    def delete_service_account(self, ctx: TenantContext, service_account_id: str) -> None:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            sa = self._load(s, ctx, service_account_id)
            s.delete(sa)
            audit.record(
                s, ctx, "service_account.delete", entity_type="service_account",
                entity_id=service_account_id,
            )

    @staticmethod
    def _validate_role(role: str) -> Role:
        try:
            role_enum = Role(role)
        except ValueError as exc:
            raise ValidationError(
                "unknown role",
                details={"role": role, "allowed": [r.value for r in _ASSIGNABLE]},
            ) from exc
        if role_enum not in _ASSIGNABLE:
            raise ValidationError(
                "role cannot be assigned to a service account",
                details={"role": role, "allowed": [r.value for r in _ASSIGNABLE]},
            )
        return role_enum

    @staticmethod
    def _load(s, ctx: TenantContext, service_account_id: str) -> ServiceAccount:  # noqa: ANN001
        sa = s.get(ServiceAccount, service_account_id)
        if sa is None or sa.group_id != ctx.group_id:
            raise NotFoundError("service account not found", details={"id": service_account_id})
        return sa


def _mint_key() -> tuple[str, str, str]:
    """Return (prefix, full_plaintext_key, sha256_hash). Prefix is also embedded in the key so
    an operator can recognise which account a leaked key belongs to."""
    prefix = "dvp_" + secrets.token_hex(4)  # 12 chars, fits String(16)
    secret = f"{prefix}_{secrets.token_hex(16)}"
    key_hash = hashlib.sha256(secret.encode()).hexdigest()
    return prefix, secret, key_hash


def _sa_dict(sa: ServiceAccount) -> dict:
    return {
        "id": sa.id,
        "name": sa.name,
        "role": sa.role,
        "key_prefix": sa.key_prefix,
        "active": sa.active,
        "last_used_at": sa.last_used_at.isoformat() if sa.last_used_at else None,
        "created_by": sa.created_by,
        "created_at": sa.created_at.isoformat() if sa.created_at else None,
    }
