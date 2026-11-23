"""OrgSettingsService — namespaced JSON settings blobs for a tenant.

Backs the Organization-settings admin screen: org identity, auth/session policy, and
governance policies (alert-silence defaults, deletion & audit retention). The UI owns the
shape of each blob; the backend stores and returns it verbatim. Reads/writes are gated on
MANAGE_GROUP and every write is audited. Secrets are never stored here.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import OrgSetting
from dvp.db.session import MetadataStore
from dvp.services import audit

# The keys the admin UI knows about. Each maps to one JSON blob per tenant.
KEYS = ("organization", "auth", "governance", "compute")


class OrgSettingsService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def get(self, ctx: TenantContext, key: str) -> dict:
        self._check_key(key)
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            row = self._load(s, ctx, key)
            return {"key": key, "value": (row.value if row else {}),
                    "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
                    "updated_by": row.updated_by if row else None}

    def set(self, ctx: TenantContext, key: str, value: dict) -> dict:
        self._check_key(key)
        ctx.require(Permission.MANAGE_GROUP)
        if not isinstance(value, dict):
            raise ValidationError("settings value must be an object", details={"key": key})
        with self.store.session(ctx) as s:
            row = self._load(s, ctx, key)
            if row is None:
                row = OrgSetting(org_id=ctx.org_id, group_id=ctx.group_id, key=key, value=value,
                                 updated_by=ctx.user_id)
                s.add(row)
            else:
                row.value = value
                row.updated_by = ctx.user_id
                row.updated_at = utcnow()
            s.flush()
            audit.record(
                s, ctx, f"settings.{key}.update", entity_type="org_setting", entity_id=row.id,
            )
            return {"key": key, "value": row.value,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "updated_by": row.updated_by}

    @staticmethod
    def _check_key(key: str) -> None:
        if key not in KEYS:
            raise ValidationError("unknown settings key", details={"key": key, "allowed": KEYS})

    @staticmethod
    def _load(s, ctx: TenantContext, key: str) -> OrgSetting | None:  # noqa: ANN001
        return s.scalar(
            select(OrgSetting).where(
                OrgSetting.group_id == ctx.group_id, OrgSetting.key == key
            )
        )
